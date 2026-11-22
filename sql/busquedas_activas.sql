-- =====================================================================================================
-- CRM CREENZIA · Búsquedas activas con alertas automáticas por coincidencia de inventario
-- Migración aplicada en Supabase como `busquedas_activas`. Es aditiva e inerte: la función nace APAGADA
-- (crm_search_settings.enabled = false) y no crea tareas hasta que un administrador la activa.
--
-- Piezas:
--   crm_search_settings          interruptor y parámetros (tipo de cambio, márgenes, guardas)
--   crm_active_searches          una búsqueda por prospecto, tomada de los criterios que ya captura el bot
--   crm_search_matches           una fila por (búsqueda, unidad C2): estado, tarea creada y resultado
--   crm_search_match_events      historial mínimo de cada coincidencia
--   crm_search_unit_snapshots    foto del inventario en el último cruce real (para explicar el motivo)
--   crm_search_runs              registro de cada cruce (real o simulación)
--   crm_search_candidates()      reglas de coincidencia (una sola fuente de verdad)
--   crm_search_cross()           cruce: detecta coincidencias nuevas, reactivadas y cambios; crea tareas
--   crm_search_cron()            lo llama pg_cron cada 5 minutos; solo cruza si la carga de inventario terminó
--   trigger en inventory_properties: marca que el inventario cambió (no hace nada más)
-- =====================================================================================================

-- ---------- Configuración ----------
create table if not exists public.crm_search_settings (
  id int primary key default 1 check (id = 1),
  enabled boolean not null default false,
  usd_rate numeric not null default 3.45,              -- el mismo tipo de cambio aproximado del Inventario C2
  fx_margin_pct numeric not null default 3,            -- cerca del límite por conversión de moneda => requiere verificación
  max_inventory_age_days int not null default 10,      -- vigencia semanal + 3 días de holgura
  settle_minutes int not null default 10,              -- espera a que la carga del inventario termine
  min_units_ratio numeric not null default 0.7,        -- si caen más de 30 % las unidades, se asume carga incompleta
  inventory_changed_at timestamptz,
  last_run_at timestamptz,
  last_run_units int,
  activated_at timestamptz,
  activated_by uuid references public.crm_users(id),
  updated_at timestamptz not null default now()
);
insert into public.crm_search_settings(id) values (1) on conflict (id) do nothing;

-- ---------- Búsquedas activas ----------
create table if not exists public.crm_active_searches (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null unique references public.crm_leads(id) on delete cascade,
  status text not null default 'activa' check (status in ('activa','pausada','cerrada')),
  operation text check (operation in ('compra','alquiler')),
  property_type text,
  districts text[] not null default '{}',
  district_keys text[] not null default '{}',
  bedrooms int check (bedrooms is null or bedrooms >= 0),
  budget_max numeric check (budget_max is null or budget_max > 0),
  currency text check (currency in ('PEN','USD')),
  payment_method text,
  delivery text check (delivery in ('inmediata','futura')),
  parking_required boolean,
  required text[] not null default '{operacion,distrito,dormitorios,presupuesto}',
  source_search_id uuid,
  bot_criteria jsonb,
  edited_by_team boolean not null default false,
  baselined_at timestamptz,
  status_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  updated_by uuid references public.crm_users(id),
  constraint crm_active_searches_required_valid
    check (required <@ array['operacion','tipo','distrito','dormitorios','presupuesto','entrega','estacionamiento'])
);

create table if not exists public.crm_search_matches (
  id uuid primary key default gen_random_uuid(),
  search_id uuid not null references public.crm_active_searches(id) on delete cascade,
  lead_id uuid not null references public.crm_leads(id) on delete cascade,
  ref text not null,
  source_reference text,
  status text not null check (status in ('coincide','verificar')),
  active boolean not null default true,
  origin text not null check (origin in ('linea_base','cambio_inventario')),
  outcome text not null default 'pendiente'
    check (outcome in ('pendiente','contactado','descartada_cliente','descartada_asesor')),
  outcome_note text,
  outcome_by uuid references public.crm_users(id),
  outcome_at timestamptz,
  reason text,
  pending text[] not null default '{}',
  snapshot jsonb,
  task_id uuid references public.crm_tasks(id) on delete set null,
  alerts int not null default 0,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  inactive_since timestamptz,
  unique (search_id, ref)
);

create table if not exists public.crm_search_match_events (
  id bigserial primary key,
  match_id uuid not null references public.crm_search_matches(id) on delete cascade,
  event text not null,
  detail text,
  at timestamptz not null default now()
);

create table if not exists public.crm_search_unit_snapshots (
  ref text primary key,
  lifecycle text,
  price numeric,
  currency text,
  bedrooms int,
  entrega text,
  estacionamiento text,
  seen_at timestamptz not null default now()
);

create table if not exists public.crm_search_runs (
  id bigserial primary key,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  mode text not null check (mode in ('real','simulacion')),
  trigger text,
  status text,
  units int,
  searches int,
  new_matches int default 0,
  tasks_created int default 0,
  suppressed int default 0,
  deactivated int default 0,
  detail text
);

create index if not exists crm_search_matches_lead on public.crm_search_matches(lead_id);
create index if not exists crm_search_match_events_match on public.crm_search_match_events(match_id);

alter table public.crm_search_settings enable row level security;
alter table public.crm_active_searches enable row level security;
alter table public.crm_search_matches enable row level security;
alter table public.crm_search_match_events enable row level security;
alter table public.crm_search_unit_snapshots enable row level security;
alter table public.crm_search_runs enable row level security;

drop policy if exists crm_search_settings_read on public.crm_search_settings;
create policy crm_search_settings_read on public.crm_search_settings for select using (public.crm_current_user_id() is not null);
drop policy if exists crm_active_searches_read on public.crm_active_searches;
create policy crm_active_searches_read on public.crm_active_searches for select using (public.crm_current_user_id() is not null);
drop policy if exists crm_search_matches_read on public.crm_search_matches;
create policy crm_search_matches_read on public.crm_search_matches for select using (public.crm_current_user_id() is not null);
drop policy if exists crm_search_match_events_read on public.crm_search_match_events;
create policy crm_search_match_events_read on public.crm_search_match_events for select using (public.crm_current_user_id() is not null);
drop policy if exists crm_search_runs_read on public.crm_search_runs;
create policy crm_search_runs_read on public.crm_search_runs for select using (public.crm_current_user_id() is not null);
-- Las escrituras se hacen solo con las funciones de abajo (SECURITY DEFINER).

-- ---------- Utilidades ----------
create or replace function public.crm_norm_district(p text)
returns text language sql immutable set search_path = '' as $$
  select nullif(regexp_replace(regexp_replace(
           lower(translate(trim(coalesce(p,'')), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun')),
           '^(santiago de |cercado de |cercado )', ''), ' del mar$', ''), '')
$$;

create or replace function public.crm_norm_ptype(p text)
returns text language sql immutable set search_path = '' as $$
  select case lower(trim(coalesce(p,'')))
    when '' then null when 'null' then null
    when 'apartment' then 'departamento' when 'department' then 'departamento' when 'flat' then 'departamento'
    when 'dpto' then 'departamento' when 'depa' then 'departamento'
    when 'house' then 'casa' when 'land' then 'terreno' when 'office' then 'oficina'
    else lower(trim(p)) end
$$;

create or replace function public.crm_search_money(p_amount numeric, p_currency text)
returns text language sql immutable set search_path = '' as $$
  select case when p_amount is null then '—'
              else (case p_currency when 'USD' then 'US$ ' else 'S/ ' end) || to_char(round(p_amount), 'FM999G999G999') end
$$;

create or replace function public.crm_search_pay_label(p text)
returns text language sql immutable set search_path = '' as $$
  select case p when 'contado' then 'al contado' when 'credito' then 'crédito hipotecario' when 'mixto' then 'contado y crédito'
                when 'por_definir' then 'por definir' when 'prefiere_no_indicar' then 'prefiere no decirlo'
                else replace(p, '_', ' ') end
$$;

create or replace function public.crm_active_searches_keys()
returns trigger language plpgsql set search_path = '' as $$
begin
  new.district_keys := coalesce((select array_agg(distinct public.crm_norm_district(d)) from unnest(new.districts) d
                                  where public.crm_norm_district(d) is not null), '{}');
  new.property_type := public.crm_norm_ptype(new.property_type);
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists crm_active_searches_keys on public.crm_active_searches;
create trigger crm_active_searches_keys before insert or update on public.crm_active_searches
  for each row execute function public.crm_active_searches_keys();

-- Requisitos obligatorios que aún no tienen valor: la búsqueda queda «pendiente de confirmación» y no se cruza.
create or replace function public.crm_search_pending(s public.crm_active_searches)
returns text[] language sql stable set search_path = '' as $$
  select coalesce(array_agg(lbl order by ord), '{}') from (values
    (1, 'operacion',      'operación (compra o alquiler)', s.operation is null),
    (2, 'tipo',           'tipo de inmueble',              s.property_type is null),
    (3, 'distrito',       'distrito',                      cardinality(s.district_keys) = 0),
    (4, 'dormitorios',    'dormitorios',                   s.bedrooms is null),
    (5, 'presupuesto',    'presupuesto y moneda',          s.budget_max is null or s.currency is null),
    (6, 'entrega',        'entrega (inmediata o futura)',  s.delivery is null),
    (7, 'estacionamiento','estacionamiento',               s.parking_required is null)
  ) v(ord, key, lbl, missing)
  where missing and key = any(s.required)
$$;

-- ---------- Criterios del bot -> búsqueda activa (sin segunda digitación) ----------
create or replace function public.crm_search_bot_criteria(p_phone text, out p_search_id uuid, out p_criteria jsonb)
language sql stable security definer set search_path = '' as $$
  select cs.search_id, cs.state->'criteria'
    from public.conversation_sessions s
    join public.conversation_searches cs on cs.search_id::text = s.state->>'active_search_id'
   where s.phone = p_phone limit 1
$$;

create or replace function public.crm_search_apply_bot(p_lead_id uuid, p_force boolean default false)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_phone text; v_sid uuid; c jsonb; v_id uuid; v_edited boolean; v_old jsonb;
        v_op text; v_cur text; v_del text; v_req text[];
begin
  select phone into v_phone from public.crm_leads where id = p_lead_id;
  if v_phone is null then return null; end if;
  select b.p_search_id, b.p_criteria into v_sid, c from public.crm_search_bot_criteria(v_phone) b;
  if c is null or jsonb_typeof(c) <> 'object' then return null; end if;

  select id, edited_by_team, bot_criteria into v_id, v_edited, v_old from public.crm_active_searches where lead_id = p_lead_id;
  if v_id is not null and v_edited and not p_force then
    update public.crm_active_searches set bot_criteria = c, source_search_id = v_sid where id = v_id;   -- solo referencia
    return v_id;
  end if;
  if v_id is not null and v_old is not distinct from c and not p_force then return v_id; end if;

  v_op  := case c->>'operation' when 'compra' then 'compra' when 'alquiler' then 'alquiler' end;
  v_cur := case upper(coalesce(c->>'currency', c->'price_offer'->>'currency')) when 'USD' then 'USD' when 'PEN' then 'PEN' end;
  v_del := case c->>'delivery_preference' when 'inmediata' then 'inmediata' when 'futura' then 'futura' end;
  v_req := array['operacion','distrito','dormitorios','presupuesto']
           || case when v_del = 'inmediata' then array['entrega'] else '{}'::text[] end
           || case when (c->>'parking_required') = 'true' then array['estacionamiento'] else '{}'::text[] end
           || case when public.crm_norm_ptype(c->>'property_type') is not null then array['tipo'] else '{}'::text[] end;

  insert into public.crm_active_searches as a (lead_id, operation, property_type, districts, bedrooms, budget_max, currency,
         payment_method, delivery, parking_required, required, source_search_id, bot_criteria, edited_by_team, baselined_at)
  values (p_lead_id, v_op, c->>'property_type',
          coalesce((select array_agg(x) from jsonb_array_elements_text(case when jsonb_typeof(c->'districts')='array' then c->'districts' else '[]' end) x), '{}'),
          case when (c->>'bedrooms') ~ '^\d+$' then (c->>'bedrooms')::int end,
          case when (c->>'budget_max') ~ '^\d+(\.\d+)?$' and (c->>'budget_max')::numeric > 0 then (c->>'budget_max')::numeric end,
          v_cur, c->>'payment_method', v_del,
          case c->>'parking_required' when 'true' then true when 'false' then false end,
          v_req, v_sid, c, false, null)
  on conflict (lead_id) do update set operation = excluded.operation, property_type = excluded.property_type,
          districts = excluded.districts, bedrooms = excluded.bedrooms, budget_max = excluded.budget_max,
          currency = excluded.currency, payment_method = excluded.payment_method, delivery = excluded.delivery,
          parking_required = excluded.parking_required, required = excluded.required,
          source_search_id = excluded.source_search_id, bot_criteria = excluded.bot_criteria,
          edited_by_team = false, baselined_at = null          -- criterios nuevos: nueva línea base, sin alertas por lo ya existente
  returning id into v_id;
  return v_id;
end $$;

create or replace function public.crm_search_sync_from_bot()
returns int language plpgsql security definer set search_path = '' as $$
declare r record; n int := 0;
begin
  for r in select l.id from public.crm_leads l
            where exists (select 1 from public.conversation_sessions s where s.phone = l.phone and s.state ? 'active_search_id') loop
    if public.crm_search_apply_bot(r.id) is not null then n := n + 1; end if;
  end loop;
  return n;
end $$;

-- ---------- Unidades vigentes del Inventario C2 ----------
create or replace function public.crm_search_units()
returns table (ref text, source_reference text, operation text, property_type text, inmobiliaria text, proyecto text,
               unidad text, distrito text, dkey text, moneda text, precio numeric, dormitorios int, entrega text,
               entrega_label text, estacionamiento text, confirmado timestamptz)
language sql stable security definer set search_path = '' as $$
  select distinct on (c.ref) c.ref, p.source_reference, p.operation, p.property_type, c.inmobiliaria, c.proyecto, c.unidad,
         c.distrito, public.crm_norm_district(c.distrito), c.moneda::text, c.precio, c.dormitorios, c.entrega,
         c.entrega_label, c.estacionamiento, c.confirmado
    from public.inventory_catalog c
    join public.inventory_properties p on p.public_reference = c.ref
   where c.flagged_state = 'active_confirmed' and p.lifecycle_state = 'active_confirmed'   -- excluye reservadas, vendidas, inactivas
   order by c.ref
$$;

-- ---------- Reglas de coincidencia ----------
-- Cada criterio da 'ok' | 'no' | 'pend' (dato de la unidad «Por confirmar») | null (la búsqueda no lo pide).
-- Obligatorio en 'no' => no coincide. Obligatorio en 'pend' => «requiere verificación». Preferencias solo se informan.
create or replace function public.crm_search_candidates(p_search_id uuid default null)
returns table (search_id uuid, lead_id uuid, ref text, source_reference text, proyecto text, unidad text, inmobiliaria text,
               distrito text, dormitorios int, moneda text, precio numeric, precio_busqueda numeric, convertido boolean,
               entrega text, estacionamiento text, confirmado timestamptz, status text, pending text[], cumple text[],
               preferencias_no text[])
language sql stable security definer set search_path = '' as $$
  with cfg as (select usd_rate, fx_margin_pct from public.crm_search_settings where id = 1),
  s as (select * from public.crm_active_searches a
        where (p_search_id is null or a.id = p_search_id) and cardinality(public.crm_search_pending(a)) = 0),
  u as (select * from public.crm_search_units()),
  ev as (
    select s.id sid, s.lead_id, s.required, s.currency scur, u.*,
      case when u.moneda = s.currency then u.precio
           when s.currency = 'PEN' and u.moneda = 'USD' then u.precio * cfg.usd_rate
           when s.currency = 'USD' and u.moneda = 'PEN' then u.precio / cfg.usd_rate end as pb,
      (s.currency is not null and u.moneda <> s.currency) as conv,
      case when s.operation is null then null when u.operation = s.operation then 'ok' else 'no' end as op_st,
      case when s.property_type is null then null when public.crm_norm_ptype(u.property_type) = s.property_type then 'ok' else 'no' end as tipo_st,
      case when cardinality(s.district_keys) = 0 then null when u.dkey = any(s.district_keys) then 'ok' else 'no' end as dist_st,
      case when s.bedrooms is null then null when u.dormitorios is null then 'pend' when u.dormitorios = s.bedrooms then 'ok' else 'no' end as dorm_st,
      case when s.delivery is null then null when s.delivery = 'futura' then 'ok'
           when u.entrega = 'Inmediata' then 'ok' when u.entrega = 'Por confirmar' then 'pend' else 'no' end as ent_st,
      case when s.parking_required is not true then null when u.estacionamiento like 'Sí%' then 'ok'
           when u.estacionamiento = 'No' then 'no' else 'pend' end as est_st,
      s.budget_max, cfg.fx_margin_pct, s.operation sop, s.bedrooms sbed, s.delivery sdel, s.parking_required spark, s.property_type stype
    from s cross join cfg
    join u on (cardinality(s.district_keys) = 0 or not ('distrito' = any(s.required)) or u.dkey = any(s.district_keys))
  ),
  ev2 as (
    select ev.*,
      case when budget_max is null then null when pb is null then 'pend'
           when pb <= budget_max then case when conv and pb > budget_max * (1 - fx_margin_pct / 100) then 'pend' else 'ok' end
           else case when conv and pb <= budget_max * (1 + fx_margin_pct / 100) then 'pend' else 'no' end end as pres_st
    from ev
  ),
  crit as (
    select ev2.*, x.key, x.st, x.lbl_ok, x.lbl_pend, x.lbl_no
    from ev2 cross join lateral (values
      ('operacion', op_st, 'operación ' || sop, null, 'operación distinta'),
      ('tipo', tipo_st, stype, null, 'otro tipo de inmueble'),
      ('distrito', dist_st, 'distrito ' || distrito, null, 'otro distrito (' || distrito || ')'),
      ('dormitorios', dorm_st, sbed || ' dorm.', 'dormitorios por confirmar', coalesce(dormitorios::text, '?') || ' dorm.'),
      ('presupuesto', pres_st, 'dentro del presupuesto (hasta ' || public.crm_search_money(budget_max, scur) || ')',
                      'precio cerca del límite por tipo de cambio', 'fuera del presupuesto'),
      ('entrega', ent_st, case when sdel = 'futura' then 'acepta entrega futura' else 'entrega inmediata' end,
                  'entrega por confirmar', 'entrega ' || coalesce(entrega, '?')),
      ('estacionamiento', est_st, 'con estacionamiento', 'estacionamiento por confirmar', 'sin estacionamiento')
    ) x(key, st, lbl_ok, lbl_pend, lbl_no)
    where x.st is not null
  )
  select sid, lead_id, ref, source_reference, proyecto, unidad, inmobiliaria, distrito, dormitorios, moneda, precio,
         round(pb), conv, entrega, estacionamiento, confirmado,
         case when bool_or(key = any(required) and st = 'pend') then 'verificar' else 'coincide' end,
         coalesce(array_agg(lbl_pend) filter (where key = any(required) and st = 'pend'), '{}'),
         coalesce(array_agg(lbl_ok) filter (where st = 'ok'), '{}'),
         coalesce(array_agg(lbl_no) filter (where not (key = any(required)) and st in ('no','pend')), '{}')
    from crit
   group by sid, lead_id, ref, source_reference, proyecto, unidad, inmobiliaria, distrito, dormitorios, moneda, precio, pb, conv,
            entrega, estacionamiento, confirmado, required
  having not bool_or(key = any(required) and st = 'no')
     and bool_or(key = any(required))                         -- al menos un requisito obligatorio evaluado
$$;

-- ---------- Elegibilidad para alertas comerciales ----------
create or replace function public.crm_search_block_reason(s public.crm_active_searches)
returns text language sql stable security definer set search_path = '' as $$
  select case
    when s.status = 'pausada' then 'búsqueda pausada'
    when s.status = 'cerrada' then 'búsqueda cerrada'
    when l.opted_out then 'No contactar'
    when l.is_test then 'prospecto de prueba'
    when l.stage_code = 'cerrado_ganado' then 'prospecto con cierre ganado'
    when cardinality(public.crm_search_pending(s)) > 0 then 'requisito obligatorio pendiente: ' || array_to_string(public.crm_search_pending(s), ', ')
  end
  from public.crm_leads l where l.id = s.lead_id
$$;

-- ---------- Cruce ----------
create or replace function public.crm_search_cross(p_dry_run boolean default true, p_alert_existing boolean default false,
                                                   p_trigger text default 'manual')
returns table (lead_id uuid, prospecto text, asesor text, ref text, unidad text, precio text, status text, accion text, motivo text, pendiente text)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
declare cfg public.crm_search_settings; v_run bigint; v_units int; v_max timestamptz; v_task uuid; r record;
        n_new int := 0; n_tasks int := 0; n_supp int := 0; n_off int := 0; v_reason text; v_detail text; v_due timestamptz;
begin
  select * into cfg from public.crm_search_settings where id = 1;
  insert into public.crm_search_runs(mode, trigger, status) values (case when p_dry_run then 'simulacion' else 'real' end, p_trigger, 'en_curso')
    returning id into v_run;

  select count(*), max(confirmado) into v_units, v_max from public.crm_search_units();
  -- Guardas ante una carga fallida o incompleta: no se crea ni se retira nada.
  if v_units = 0 or v_max < now() - make_interval(days => cfg.max_inventory_age_days) then
    update public.crm_search_runs set finished_at = now(), status = 'omitido', units = v_units,
           detail = 'Inventario vencido o vacío (última confirmación ' || coalesce(to_char(v_max at time zone 'America/Lima','DD/MM/YYYY'),'—') || ')'
     where id = v_run;
    return;
  end if;
  if cfg.last_run_units is not null and v_units < cfg.last_run_units * cfg.min_units_ratio then
    update public.crm_search_runs set finished_at = now(), status = 'omitido', units = v_units,
           detail = format('Posible carga incompleta: %s unidades vigentes frente a %s en el cruce anterior', v_units, cfg.last_run_units)
     where id = v_run;
    return;
  end if;

  drop table if exists _cand;
  -- Una sola pasada por el inventario para todas las búsquedas elegibles
  create temp table _cand on commit drop as
    select c.*, s.baselined_at
      from public.crm_search_candidates(null) c
      join public.crm_active_searches s on s.id = c.search_id
     where public.crm_search_block_reason(s) is null;
  drop table if exists _act;
  create temp table _act (match_id uuid, search_id uuid, lead_id uuid, ref text, kind text, accion text, motivo text) on commit drop;
  -- Búsquedas no elegibles (pausada, cerrada, No contactar, prueba, requisito pendiente…): no se cruzan ni avisan
  insert into _act
    select null, s.id, s.lead_id, null, 'suprimida', 'búsqueda no evaluada: ' || public.crm_search_block_reason(s), null
      from public.crm_active_searches s where public.crm_search_block_reason(s) is not null;
  get diagnostics n_supp = row_count;

  -- 1) Pares que hoy cumplen
  for r in
    select c.*, m.id mid, m.active m_active, m.status m_status, m.outcome m_outcome, m.snapshot m_snap,
           sn.lifecycle sn_life, sn.price sn_price, sn.currency sn_cur, sn.bedrooms sn_bed, sn.entrega sn_ent, sn.estacionamiento sn_est
      from _cand c
      left join public.crm_search_matches m on m.search_id = c.search_id and m.ref = c.ref
      left join public.crm_search_unit_snapshots sn on sn.ref = c.ref
  loop
    -- Motivo: qué cambió en la unidad desde el último cruce real
    v_reason := case
      when r.sn_life is null then 'Unidad nueva en el inventario'
      when r.sn_life <> 'active_confirmed' then 'La unidad vuelve a estar disponible'
      when r.sn_price is distinct from r.precio or r.sn_cur is distinct from r.moneda then
        format('Cambió de precio: de %s a %s', public.crm_search_money(r.sn_price, r.sn_cur), public.crm_search_money(r.precio, r.moneda))
      when r.sn_ent is distinct from r.entrega then format('Cambió la entrega: de %s a %s', coalesce(r.sn_ent,'—'), coalesce(r.entrega,'—'))
      when r.sn_bed is distinct from r.dormitorios then format('Cambiaron los dormitorios: de %s a %s', coalesce(r.sn_bed::text,'—'), coalesce(r.dormitorios::text,'—'))
      when r.sn_est is distinct from r.estacionamiento then format('Cambió el estacionamiento: de %s a %s', coalesce(r.sn_est,'—'), coalesce(r.estacionamiento,'—'))
      else 'Ahora cumple la búsqueda' end;

    if r.mid is null then
      if r.baselined_at is null and not p_alert_existing then
        insert into _act values (null, r.search_id, r.lead_id, r.ref, 'linea_base', 'línea base (ya existía; sin tarea)', null);
      else
        insert into _act values (null, r.search_id, r.lead_id, r.ref, 'nueva', 'tarea', v_reason);
        n_new := n_new + 1;
      end if;
    elsif not r.m_active then
      if r.m_outcome = 'descartada_cliente' then
        insert into _act values (r.mid, r.search_id, r.lead_id, r.ref, 'reactivada_sin_aviso', 'vuelve a cumplir (descartada por el cliente; sin tarea)', v_reason);
      else
        insert into _act values (r.mid, r.search_id, r.lead_id, r.ref, 'reactivada', 'tarea', v_reason);
        n_new := n_new + 1;
      end if;
    else
      insert into _act values (r.mid, r.search_id, r.lead_id, r.ref,
        case when r.m_status = 'verificar' and r.status = 'coincide' then 'confirmada' else 'sigue' end,
        case when r.m_status = 'verificar' and r.status = 'coincide' then 'ya avisada; ahora confirmada (sin tarea nueva)' else 'ya avisada (sin tarea nueva)' end, null);
    end if;
  end loop;

  -- 2) Coincidencias que dejaron de cumplir (solo en búsquedas elegibles: pausar no borra el historial)
  insert into _act
    select m.id, m.search_id, m.lead_id, m.ref, 'deja', 'deja de cumplir', null
      from public.crm_search_matches m join public.crm_active_searches s on s.id = m.search_id
     where m.active and public.crm_search_block_reason(s) is null
       and not exists (select 1 from _cand c where c.search_id = m.search_id and c.ref = m.ref);
  get diagnostics n_off = row_count;

  if not p_dry_run then
    v_due := now() + interval '1 day';
    for r in
      select a.*, c.status cstatus, c.pending cpending, c.cumple, c.preferencias_no, c.proyecto, c.unidad, c.distrito, c.dormitorios,
             c.moneda, c.precio, c.precio_busqueda, c.convertido, c.entrega, c.estacionamiento, c.confirmado, c.source_reference,
             c.inmobiliaria, s.currency scur, s.budget_max, s.payment_method,
             l.full_name, l.phone, coalesce(l.assigned_to, (select id from public.crm_users where role = 'admin' and active order by created_at limit 1)) owner
        from _act a
        left join _cand c on c.search_id = a.search_id and c.ref = a.ref
        join public.crm_active_searches s on s.id = a.search_id
        join public.crm_leads l on l.id = a.lead_id
       where a.kind <> 'suprimida'
    loop
      if r.kind = 'deja' then
        update public.crm_search_matches set active = false, inactive_since = now() where id = r.match_id;
        insert into public.crm_search_match_events(match_id, event, detail) values (r.match_id, 'deja_de_cumplir', null);
        continue;
      end if;

      if r.kind in ('linea_base','nueva') then
        insert into public.crm_search_matches(search_id, lead_id, ref, source_reference, status, origin, reason, pending, snapshot)
        values (r.search_id, r.lead_id, r.ref, r.source_reference, r.cstatus,
                case when r.kind = 'linea_base' then 'linea_base' else 'cambio_inventario' end, r.motivo, r.cpending,
                jsonb_build_object('precio', r.precio, 'moneda', r.moneda, 'proyecto', r.proyecto, 'unidad', r.unidad, 'confirmado', r.confirmado))
        on conflict (search_id, ref) do nothing
        returning id into r.match_id;
        if r.match_id is null then continue; end if;             -- otro proceso ya la registró: sin duplicados
        insert into public.crm_search_match_events(match_id, event, detail) values (r.match_id, r.kind, r.motivo);
      else
        update public.crm_search_matches set active = true, inactive_since = null, last_seen_at = now(), status = r.cstatus,
               pending = r.cpending, reason = coalesce(r.motivo, reason), source_reference = r.source_reference,
               snapshot = jsonb_build_object('precio', r.precio, 'moneda', r.moneda, 'proyecto', r.proyecto, 'unidad', r.unidad, 'confirmado', r.confirmado)
         where id = r.match_id;
        if r.kind in ('reactivada','reactivada_sin_aviso','confirmada') then
          insert into public.crm_search_match_events(match_id, event, detail) values (r.match_id, r.kind, r.motivo);
        end if;
      end if;

      if r.kind in ('nueva','reactivada') and cfg.enabled then
        v_detail := concat_ws(E'\n',
          'Prospecto: ' || coalesce(r.full_name, '+' || r.phone),
          'Asesor: ' || coalesce((select full_name from public.crm_users where id = r.owner), '—'),
          'Unidad: ' || r.ref || ' · ' || concat_ws(', ', 'Proyecto ' || r.proyecto, 'unidad ' || r.unidad) || ' · ' || r.distrito
                     || coalesce(' · ' || r.dormitorios || ' dorm.', ''),
          'Precio: ' || public.crm_search_money(r.precio, r.moneda) || ' (precio original del inventario'
                     || case when r.convertido then format('; ≈ %s, conversión aproximada a S/ %s por US$)', public.crm_search_money(r.precio_busqueda, r.scur), cfg.usd_rate) else ')' end,
          'Motivo: ' || r.motivo || '.',
          'Cumple: ' || array_to_string(r.cumple, ' · ') || '.',
          case when cardinality(r.preferencias_no) > 0 then 'Preferencias no cumplidas: ' || array_to_string(r.preferencias_no, ', ') || '.' end,
          case when cardinality(r.cpending) > 0 then 'Dato pendiente: ' || array_to_string(r.cpending, ', ') || ' (requiere verificación, no es coincidencia confirmada).' end,
          'Entrega: ' || coalesce(r.entrega, '—') || ' · Estacionamiento: ' || coalesce(r.estacionamiento, '—')
                     || coalesce(' · Forma de pago del cliente: ' || public.crm_search_pay_label(r.payment_method), ''),
          'Fecha del inventario: ' || to_char(r.confirmado at time zone 'America/Lima', 'DD/MM/YYYY'),
          'Antes de contactar: reconfirma la unidad con la fuente y decide si corresponde escribir al cliente.');
        insert into public.crm_tasks(lead_id, assigned_to, title, detail, due_at)
        values (r.lead_id, r.owner,
                case when r.cstatus = 'verificar' then 'Inventario: verificar ' || r.ref || ' para su búsqueda'
                     else 'Inventario: ' || r.ref || ' coincide con su búsqueda' end,
                v_detail, v_due)
        returning id into v_task;
        update public.crm_search_matches set task_id = v_task, alerts = alerts + 1 where id = r.match_id;
        insert into public.crm_search_match_events(match_id, event, detail) values (r.match_id, 'tarea_creada', v_task::text);
        n_tasks := n_tasks + 1;
      end if;
    end loop;

    update public.crm_active_searches s set baselined_at = now()
     where s.baselined_at is null and public.crm_search_block_reason(s) is null;
    -- Una búsqueda bloqueada pierde su línea base: al reanudarse no avisa lo acumulado mientras estuvo bloqueada.
    update public.crm_active_searches s set baselined_at = null
     where s.baselined_at is not null and public.crm_search_block_reason(s) is not null;

    -- Foto del inventario para explicar el próximo motivo (incluye unidades no vigentes)
    insert into public.crm_search_unit_snapshots(ref, lifecycle, price, currency, bedrooms, entrega, estacionamiento, seen_at)
      select p.public_reference, p.lifecycle_state, p.price_amount, p.currency, p.bedrooms, u.entrega, u.estacionamiento, now()
        from public.inventory_properties p left join public.crm_search_units() u on u.ref = p.public_reference
       where p.public_reference is not null
    on conflict (ref) do update set lifecycle = excluded.lifecycle, price = excluded.price, currency = excluded.currency,
       bedrooms = excluded.bedrooms, entrega = excluded.entrega, estacionamiento = excluded.estacionamiento, seen_at = excluded.seen_at;

    update public.crm_search_settings set last_run_at = now(), last_run_units = v_units where id = 1;
  end if;

  update public.crm_search_runs set finished_at = now(), status = 'ok', units = v_units,
         searches = (select count(distinct search_id) from _cand), new_matches = n_new,
         tasks_created = n_tasks, suppressed = n_supp, deactivated = n_off,
         detail = case when p_dry_run then 'Simulación: no se guardó nada' when not cfg.enabled then 'Alertas apagadas: sin tareas' end
   where id = v_run;

  return query
    select a.lead_id, coalesce(l.full_name, '+' || l.phone), coalesce(cu.full_name, '—'), a.ref,
           concat_ws(' ', c.proyecto, c.unidad), public.crm_search_money(c.precio, c.moneda), c.status, a.accion, a.motivo,
           array_to_string(c.pending, ', ')
      from _act a join public.crm_leads l on l.id = a.lead_id
      left join public.crm_users cu on cu.id = coalesce(l.assigned_to, (select id from public.crm_users where role = 'admin' and active order by created_at limit 1))
      left join _cand c on c.search_id = a.search_id and c.ref = a.ref
     order by (a.accion = 'tarea') desc, 2, 4;
end $$;

-- ---------- Programación: inventario actualizado -> cruce ----------
create or replace function public.crm_inventory_changed()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  begin
    update public.crm_search_settings set inventory_changed_at = now() where id = 1;
  exception when others then null;   -- nunca debe frenar una carga de inventario
  end;
  return null;
end $$;
drop trigger if exists crm_inventory_changed on public.inventory_properties;
create trigger crm_inventory_changed after insert or update or delete on public.inventory_properties
  for each statement execute function public.crm_inventory_changed();

create or replace function public.crm_search_cron()
returns text language plpgsql security definer set search_path = '' as $$
declare cfg public.crm_search_settings;
begin
  select * into cfg from public.crm_search_settings where id = 1;
  if not cfg.enabled then return 'apagado'; end if;
  perform public.crm_search_sync_from_bot();
  if cfg.inventory_changed_at is not null and cfg.inventory_changed_at > now() - make_interval(mins => cfg.settle_minutes) then
    return 'esperando que termine la carga de inventario';
  end if;
  if (cfg.inventory_changed_at is not null and cfg.inventory_changed_at > coalesce(cfg.last_run_at, '-infinity'))
     or exists (select 1 from public.crm_active_searches s where s.baselined_at is null and public.crm_search_block_reason(s) is null)
     or cfg.last_run_at is null or cfg.last_run_at < now() - interval '1 day' then
    perform public.crm_search_cross(false, false, 'programado');
    return 'cruce ejecutado';
  end if;
  return 'sin cambios';
end $$;

-- ---------- Funciones para el CRM (requieren usuario del CRM) ----------
create or replace function public.crm_search_get(p_lead_id uuid)
returns json language plpgsql security definer set search_path = '' as $$
declare s public.crm_active_searches;
begin
  if public.crm_current_user_id() is null then raise exception 'No autorizado'; end if;
  select * into s from public.crm_active_searches where lead_id = p_lead_id;
  if s.id is null then
    perform public.crm_search_apply_bot(p_lead_id);
    select * into s from public.crm_active_searches where lead_id = p_lead_id;
  end if;
  return json_build_object(
    'settings', (select json_build_object('enabled', enabled, 'usd_rate', usd_rate, 'last_run_at', last_run_at) from public.crm_search_settings where id = 1),
    'search', case when s.id is null then null else row_to_json(s) end,
    'pending', case when s.id is null then '[]'::json else to_json(public.crm_search_pending(s)) end,
    'block', case when s.id is null then null else public.crm_search_block_reason(s) end,
    'candidates', case when s.id is null then '[]'::json else coalesce((
        select json_agg(json_build_object('ref', c.ref, 'proyecto', c.proyecto, 'unidad', c.unidad, 'distrito', c.distrito,
               'dormitorios', c.dormitorios, 'precio', public.crm_search_money(c.precio, c.moneda),
               'precio_busqueda', case when c.convertido then public.crm_search_money(c.precio_busqueda, s.currency) end,
               'entrega', c.entrega, 'estacionamiento', c.estacionamiento, 'status', c.status, 'pending', c.pending,
               'preferencias_no', c.preferencias_no, 'confirmado', c.confirmado,
               'match_id', m.id, 'outcome', m.outcome, 'task_id', m.task_id)
               order by c.status, c.precio)
          from public.crm_search_candidates(s.id) c
          left join public.crm_search_matches m on m.search_id = s.id and m.ref = c.ref), '[]'::json) end,
    'history', coalesce((select json_agg(json_build_object('match_id', m.id, 'ref', m.ref, 'status', m.status, 'active', m.active,
               'origin', m.origin, 'outcome', m.outcome, 'reason', m.reason, 'task_id', m.task_id, 'alerts', m.alerts,
               'snapshot', m.snapshot, 'first_seen_at', m.first_seen_at,
               'events', (select json_agg(json_build_object('event', e.event, 'detail', e.detail, 'at', e.at) order by e.at)
                            from public.crm_search_match_events e where e.match_id = m.id))
               order by m.first_seen_at desc)
          from public.crm_search_matches m where m.lead_id = p_lead_id and (m.origin = 'cambio_inventario' or m.outcome <> 'pendiente')), '[]'::json)
  );
end $$;

create or replace function public.crm_search_save(p_lead_id uuid, p jsonb)
returns void language plpgsql security definer set search_path = '' as $$
declare v_user uuid := public.crm_current_user_id();
begin
  if v_user is null then raise exception 'No autorizado'; end if;
  insert into public.crm_active_searches as a (lead_id, operation, property_type, districts, bedrooms, budget_max, currency,
         payment_method, delivery, parking_required, required, edited_by_team, baselined_at, updated_by)
  values (p_lead_id, nullif(p->>'operation',''), nullif(p->>'property_type',''),
          coalesce((select array_agg(trim(x)) from jsonb_array_elements_text(coalesce(p->'districts','[]')) x where trim(x) <> ''), '{}'),
          nullif(p->>'bedrooms','')::int, nullif(p->>'budget_max','')::numeric, nullif(p->>'currency',''),
          nullif(p->>'payment_method',''), nullif(p->>'delivery',''),
          case p->>'parking_required' when 'true' then true when 'false' then false end,
          coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(p->'required','[]')) x), '{}'),
          true, null, v_user)
  on conflict (lead_id) do update set operation = excluded.operation, property_type = excluded.property_type,
          districts = excluded.districts, bedrooms = excluded.bedrooms, budget_max = excluded.budget_max, currency = excluded.currency,
          payment_method = excluded.payment_method, delivery = excluded.delivery, parking_required = excluded.parking_required,
          required = excluded.required, edited_by_team = true, updated_by = v_user,
          baselined_at = case when (a.operation, a.property_type, a.districts, a.bedrooms, a.budget_max, a.currency, a.delivery, a.parking_required, a.required)
                                   is not distinct from
                                   (excluded.operation, public.crm_norm_ptype(excluded.property_type), excluded.districts, excluded.bedrooms, excluded.budget_max,
                                    excluded.currency, excluded.delivery, excluded.parking_required, excluded.required)
                              then a.baselined_at else null end;
end $$;

create or replace function public.crm_search_set_status(p_lead_id uuid, p_status text, p_reason text default null)
returns void language plpgsql security definer set search_path = '' as $$
declare v_user uuid := public.crm_current_user_id();
begin
  if v_user is null then raise exception 'No autorizado'; end if;
  if p_status not in ('activa','pausada','cerrada') then raise exception 'Estado inválido'; end if;
  update public.crm_active_searches
     set status = p_status, status_reason = nullif(p_reason, ''), updated_by = v_user,
         baselined_at = case when p_status = 'activa' and status <> 'activa' then null else baselined_at end   -- al reanudar: nueva línea base
   where lead_id = p_lead_id;
end $$;

create or replace function public.crm_search_reset_from_bot(p_lead_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if public.crm_current_user_id() is null then raise exception 'No autorizado'; end if;
  perform public.crm_search_apply_bot(p_lead_id, true);
end $$;

create or replace function public.crm_search_set_outcome(p_match_id uuid, p_outcome text, p_note text default null)
returns void language plpgsql security definer set search_path = '' as $$
declare v_user uuid := public.crm_current_user_id();
begin
  if v_user is null then raise exception 'No autorizado'; end if;
  if p_outcome not in ('pendiente','contactado','descartada_cliente','descartada_asesor') then raise exception 'Resultado inválido'; end if;
  update public.crm_search_matches set outcome = p_outcome, outcome_note = nullif(p_note,''), outcome_by = v_user, outcome_at = now()
   where id = p_match_id;
  insert into public.crm_search_match_events(match_id, event, detail) values (p_match_id, 'resultado:' || p_outcome, nullif(p_note,''));
end $$;

-- Administración (solo administrador)
create or replace function public.crm_search_admin_status()
returns json language plpgsql security definer set search_path = '' as $$
begin
  if public.crm_current_user_id() is null then raise exception 'No autorizado'; end if;
  return json_build_object(
    'settings', (select row_to_json(x) from public.crm_search_settings x where id = 1),
    'is_admin', public.crm_is_admin(),
    'searches', (select json_build_object('activas', count(*) filter (where status = 'activa'), 'pausadas', count(*) filter (where status = 'pausada'),
                                          'cerradas', count(*) filter (where status = 'cerrada')) from public.crm_active_searches),
    'runs', (select json_agg(r order by r.id desc) from (select * from public.crm_search_runs order by id desc limit 5) r));
end $$;

create or replace function public.crm_search_simulate(p_alert_existing boolean default false)
returns json language plpgsql security definer set search_path = '' as $$
begin
  if not public.crm_is_admin() then raise exception 'Solo el administrador'; end if;
  perform public.crm_search_sync_from_bot();
  return coalesce((select json_agg(row_to_json(x)) from public.crm_search_cross(true, p_alert_existing, 'simulacion_crm') x), '[]'::json);
end $$;

create or replace function public.crm_search_activate(p_alert_existing boolean default false)
returns json language plpgsql security definer set search_path = '' as $$
declare v_user uuid := public.crm_current_user_id(); v_res json;
begin
  if not public.crm_is_admin() then raise exception 'Solo el administrador'; end if;
  update public.crm_search_settings set enabled = true, activated_at = now(), activated_by = v_user, updated_at = now() where id = 1;
  perform public.crm_search_sync_from_bot();
  select coalesce(json_agg(row_to_json(x)), '[]'::json) into v_res from public.crm_search_cross(false, p_alert_existing, 'activacion') x;
  return v_res;
end $$;

create or replace function public.crm_search_deactivate()
returns void language plpgsql security definer set search_path = '' as $$
begin
  if not public.crm_is_admin() then raise exception 'Solo el administrador'; end if;
  update public.crm_search_settings set enabled = false, updated_at = now() where id = 1;
end $$;

-- ---------- Permisos ----------
do $$
declare f text;
begin
  foreach f in array array[
    'crm_norm_district(text)','crm_norm_ptype(text)','crm_search_money(numeric,text)','crm_search_pay_label(text)','crm_active_searches_keys()',
    'crm_search_pending(public.crm_active_searches)','crm_search_bot_criteria(text)','crm_search_apply_bot(uuid,boolean)',
    'crm_search_sync_from_bot()','crm_search_units()','crm_search_candidates(uuid)','crm_search_block_reason(public.crm_active_searches)',
    'crm_search_cross(boolean,boolean,text)','crm_inventory_changed()','crm_search_cron()',
    'crm_search_get(uuid)','crm_search_save(uuid,jsonb)','crm_search_set_status(uuid,text,text)','crm_search_reset_from_bot(uuid)',
    'crm_search_set_outcome(uuid,text,text)','crm_search_admin_status()','crm_search_simulate(boolean)',
    'crm_search_activate(boolean)','crm_search_deactivate()'] loop
    execute format('revoke all on function public.%s from public, anon, authenticated', f);
    execute format('grant execute on function public.%s to service_role', f);
  end loop;
  foreach f in array array[
    'crm_search_get(uuid)','crm_search_save(uuid,jsonb)','crm_search_set_status(uuid,text,text)','crm_search_reset_from_bot(uuid)',
    'crm_search_set_outcome(uuid,text,text)','crm_search_admin_status()','crm_search_simulate(boolean)',
    'crm_search_activate(boolean)','crm_search_deactivate()'] loop
    execute format('grant execute on function public.%s to authenticated', f);
  end loop;
end $$;
revoke all on public.crm_search_settings, public.crm_active_searches, public.crm_search_matches, public.crm_search_match_events,
              public.crm_search_unit_snapshots, public.crm_search_runs from anon;
grant select on public.crm_search_settings, public.crm_active_searches, public.crm_search_matches, public.crm_search_match_events,
                public.crm_search_runs to authenticated;
revoke insert, update, delete on public.crm_search_settings, public.crm_active_searches, public.crm_search_matches,
              public.crm_search_match_events, public.crm_search_runs from authenticated;

-- ---------- pg_cron (el mismo que ya usa el CRM; costo 0). Con la función apagada solo responde 'apagado'. ----------
select cron.unschedule(jobid) from cron.job where jobname = 'crm-busquedas-activas';
select cron.schedule('crm-busquedas-activas', '*/5 * * * *', $$select public.crm_search_cron()$$);
