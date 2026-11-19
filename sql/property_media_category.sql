-- 19/11: cada foto de una unidad conserva la categoría de su proyecto (foto, render, piloto, plano, general).
-- El bot no envía renders a unidades de entrega inmediata (InventoryStore.photos_for_reference).
-- Aplicada en Supabase como migración property_media_category.
alter table inventory_property_media add column if not exists category text;

CREATE OR REPLACE FUNCTION public.sync_property_media_from_projects()
 RETURNS integer LANGUAGE plpgsql SET search_path TO 'public'
AS $function$
declare n integer;
begin
  delete from inventory_property_media where source_name = 'project_photos';
  insert into inventory_property_media(property_id, media_type, media_url, display_order, approved_for_client, approved_at, source_name, category)
  select p.property_id, 'photo', m.media_url, m.display_order, m.approved_for_client, m.approved_at, 'project_photos', m.category
  from inventory_project_media m
  join inventory_project_media_keys k on k.trello_short_link = m.trello_short_link
  join inventory_properties p on public.inventory_property_project_key(p.source_reference) = k.project_key
  where p.lifecycle_state = 'active_confirmed'
    and (m.bedrooms is null or m.bedrooms = p.bedrooms);
  get diagnostics n = row_count;
  return n;
end $function$;

-- Park Prado (entregado): sus 8 imágenes estaban como 'foto' pero no son reales (confirmado por ARENZ el 18/11).
update inventory_project_media set category = 'render' where project_slug = 'park-prado' and category = 'foto';
select public.sync_property_media_from_projects();
