-- Aplicado en Supabase (proyecto arenz-ai-whatsapp) el 18/11/2026 con las migraciones
-- photo_batch_followups y photo_batch_followups_early_status.
--
-- La pregunta que va debajo de una tanda de fotos se envía cuando WhatsApp confirma que llegaron todas,
-- porque WhatsApp no garantiza el orden de entrega (13/11: la pregunta apareció sobre la primera foto).

create table if not exists public.photo_batch_followups (
  id uuid primary key default gen_random_uuid(),
  phone text not null,
  body text not null,
  pending_message_ids text[] not null,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index if not exists photo_batch_followups_pending_idx
  on public.photo_batch_followups using gin (pending_message_ids) where sent_at is null;
alter table public.photo_batch_followups enable row level security;
revoke all on public.photo_batch_followups from anon, authenticated;

-- Avisos de entrega que llegan antes de que la tanda quede registrada.
create table if not exists public.photo_delivery_seen (
  message_id text primary key,
  phone text not null,
  seen_at timestamptz not null default now()
);
alter table public.photo_delivery_seen enable row level security;
revoke all on public.photo_delivery_seen from anon, authenticated;

-- Un aviso de entrega (o fallo) de una foto. Devuelve la pregunta una sola vez, cuando no queda ninguna foto pendiente.
create or replace function public.photo_batch_delivered(p_phone text, p_message_id text)
returns table(phone text, body text)
language plpgsql security definer set search_path to 'public'
as $$
declare r public.photo_batch_followups;
begin
  perform pg_advisory_xact_lock(hashtext('photo_batch:' || p_phone));
  update public.photo_batch_followups f
     set pending_message_ids = array_remove(f.pending_message_ids, p_message_id)
   where f.phone = p_phone and f.sent_at is null and p_message_id = any(f.pending_message_ids)
  returning * into r;
  if r.id is null then
    insert into public.photo_delivery_seen(message_id, phone) values (p_message_id, p_phone) on conflict do nothing;
    return;
  end if;
  if cardinality(r.pending_message_ids) > 0 then
    return;
  end if;
  update public.photo_batch_followups f set sent_at = now() where f.id = r.id and f.sent_at is null
  returning f.phone, f.body into phone, body;
  if found then return next; end if;
end $$;

-- Registra la tanda recién enviada. Devuelve true si todas sus fotos ya se entregaron (enviar la pregunta ya).
create or replace function public.photo_batch_create(p_phone text, p_body text, p_message_ids text[])
returns boolean
language plpgsql security definer set search_path to 'public'
as $$
declare remaining text[];
begin
  perform pg_advisory_xact_lock(hashtext('photo_batch:' || p_phone));
  delete from public.photo_delivery_seen where seen_at < now() - interval '1 day';
  remaining := array(select m from unnest(p_message_ids) m
                     where m not in (select s.message_id from public.photo_delivery_seen s where s.phone = p_phone));
  delete from public.photo_delivery_seen s where s.phone = p_phone and s.message_id = any(p_message_ids);
  insert into public.photo_batch_followups(phone, body, pending_message_ids, sent_at)
  values (p_phone, p_body, remaining, case when cardinality(remaining) = 0 then now() end);
  return cardinality(remaining) = 0;
end $$;

revoke all on function public.photo_batch_delivered(text, text) from anon, authenticated, public;
revoke all on function public.photo_batch_create(text, text, text[]) from anon, authenticated, public;
