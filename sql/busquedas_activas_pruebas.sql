-- Pruebas de las búsquedas activas con DATOS FICTICIOS sobre la base real, sin dejar rastro:
-- todo corre dentro de un bloque que termina con RAISE EXCEPTION, así PostgreSQL deshace cada cambio.
-- El informe aparece como mensaje de error ("RESULTADO DE PRUEBAS …"): ese es el resultado esperado.
do $$
declare
  v_cec uuid := (select id from public.crm_users where full_name = 'Cecilia' limit 1);
  v_adm uuid := (select id from public.crm_users where role = 'admin' and active order by created_at limit 1);
  l1 uuid; l2 uuid; l3 uuid; l4 uuid; l5 uuid; l6 uuid;
  s1 uuid; m1 uuid; n int; t text; rep text := ''; ok int := 0; fail int := 0;
  base_units int;
begin
  update public.crm_search_settings set enabled = true, last_run_units = null, last_run_at = null where id = 1;

  -- Prospectos ficticios (Cecilia es la asesora responsable)
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test)
    values ('51900000001', 'PRUEBA Ana Lince', 'nuevo', v_cec, 'ORG', false) returning id into l1;
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test)
    values ('51900000002', 'PRUEBA Beto sin dormitorios', 'nuevo', v_cec, 'ORG', false) returning id into l2;
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test)
    values ('51900000003', 'PRUEBA Carla cerrada', 'nuevo', v_cec, 'ORG', false) returning id into l3;
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test, opted_out, opted_out_at)
    values ('51900000004', 'PRUEBA Dante No contactar', 'nuevo', v_cec, 'ORG', false, true, now()) returning id into l4;
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test)
    values ('51900000005', 'PRUEBA Elena con cochera', 'nuevo', v_adm, 'ORG', false) returning id into l5;
  insert into public.crm_leads(phone, full_name, stage_code, assigned_to, campaign_code, is_test)
    values ('51900000006', 'PRUEBA Fito en dólares', 'nuevo', v_cec, 'ORG', false) returning id into l6;

  -- Búsquedas: 2 dorm. en Lince, entrega inmediata, hasta S/ 450 000
  insert into public.crm_active_searches(lead_id, operation, property_type, districts, bedrooms, budget_max, currency, delivery, payment_method, required)
    values (l1, 'compra', 'departamento', '{Lince}', 2, 450000, 'PEN', 'inmediata', 'credito', '{operacion,tipo,distrito,dormitorios,presupuesto,entrega}')
    returning id into s1;
  insert into public.crm_active_searches(lead_id, operation, districts, bedrooms, budget_max, currency, delivery, required)
    values (l2, 'compra', '{Lince}', null, 450000, 'PEN', 'inmediata', '{operacion,distrito,dormitorios,presupuesto,entrega}');
  insert into public.crm_active_searches(lead_id, status, operation, districts, bedrooms, budget_max, currency, delivery, required)
    values (l3, 'cerrada', 'compra', '{Lince}', 2, 450000, 'PEN', 'inmediata', '{operacion,distrito,dormitorios,presupuesto,entrega}');
  insert into public.crm_active_searches(lead_id, operation, districts, bedrooms, budget_max, currency, delivery, required)
    values (l4, 'compra', '{Lince}', 2, 450000, 'PEN', 'inmediata', '{operacion,distrito,dormitorios,presupuesto,entrega}');
  insert into public.crm_active_searches(lead_id, operation, districts, bedrooms, budget_max, currency, delivery, parking_required, required)
    values (l5, 'compra', '{Lince}', 2, 450000, 'PEN', 'inmediata', true, '{operacion,distrito,dormitorios,presupuesto,entrega,estacionamiento}');
  insert into public.crm_active_searches(lead_id, operation, districts, bedrooms, budget_max, currency, delivery, required)
    values (l6, 'compra', '{Lince}', 2, 130000, 'USD', 'inmediata', '{operacion,distrito,dormitorios,presupuesto,entrega}');

  -- Unidad ficticia: S/ 465 000 (fuera de presupuesto) + otra reservada que sí calzaría
  insert into public.inventory_properties(public_reference, source_reference, operation, property_type, district, price_amount, currency,
         bedrooms, features, public_description, lifecycle_state, availability_confirmed_at, approved_at, verification_status,
         provenance, source_name)
  values ('C2-PRUEBA00001', 'lista:c2:pruebalince::101', 'compra', 'departamento', 'Lince', 465000, 'PEN', 2, '[]',
          'Proyecto PRUEBA LINCE; unidad 101. Piso 1. Entrega según fuente: Entregado.', 'active_confirmed', now(), now(), 'verified', 'own', 'lista_precios'),
         ('C2-PRUEBA00002', 'lista:c2:pruebalince::102', 'compra', 'departamento', 'Lince', 430000, 'PEN', 2, '[]',
          'Proyecto PRUEBA LINCE; unidad 102. Piso 1. Entrega según fuente: Entregado.', 'reserved', now(), now(), 'verified', 'own', 'lista_precios');

  -- Cruce 0: línea base (nada coincide todavía)
  perform public.crm_search_cross(false, false, 'prueba');
  select count(*) into n from public.crm_tasks where lead_id in (l1,l2,l3,l4,l5,l6);
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   línea base: 0 tareas con S/ 465 000';
  else fail := fail + 1; rep := rep || E'\nFAIL línea base: ' || n || ' tareas'; end if;

  -- Baja de precio a S/ 440 000
  update public.inventory_properties set price_amount = 440000, updated_at = now() where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  -- Se procesa la actualización dos veces (misma carga repetida + reintento)
  update public.inventory_properties set price_amount = 440000, updated_at = now() where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  perform public.crm_search_cross(false, false, 'prueba');

  select count(*) into n from public.crm_tasks where lead_id = l1;
  select string_agg(title || ' | asesor=' || coalesce(assigned_to::text,'∅') || ' | ' || detail, ' // ') into t from public.crm_tasks where lead_id = l1;
  if n = 1 and (select assigned_to from public.crm_tasks where lead_id = l1) = v_cec
     and t like '%C2-PRUEBA00001%' and t like '%Cambió de precio: de S/ 465,000 a S/ 440,000%' and t like '%Fecha del inventario%' then
    ok := ok + 1; rep := rep || E'\nOK   bajada de precio procesada 3 veces: exactamente 1 tarea para Cecilia con referencia, motivo y fecha';
  else fail := fail + 1; rep := rep || E'\nFAIL bajada de precio: ' || n || ' tareas: ' || coalesce(t,'—'); end if;

  select count(*) into n from public.crm_tasks where lead_id = l1 and detail like '%C2-PRUEBA00002%';
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   unidad reservada: sin tarea';
  else fail := fail + 1; rep := rep || E'\nFAIL unidad reservada generó tarea'; end if;

  select count(*) into n from public.crm_tasks where lead_id = l2;
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   requisito obligatorio pendiente (dormitorios): sin tarea; bloqueo = '
                 || (select public.crm_search_block_reason(s) from public.crm_active_searches s where lead_id = l2);
  else fail := fail + 1; rep := rep || E'\nFAIL requisito pendiente generó tarea'; end if;

  select count(*) into n from public.crm_tasks where lead_id = l3;
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   búsqueda cerrada: sin tarea';
  else fail := fail + 1; rep := rep || E'\nFAIL búsqueda cerrada generó tarea'; end if;

  select count(*) into n from public.crm_tasks where lead_id = l4;
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   «No contactar»: sin tarea';
  else fail := fail + 1; rep := rep || E'\nFAIL No contactar generó tarea'; end if;

  select string_agg(title || ' | ' || detail, ' // ') into t from public.crm_tasks where lead_id = l5;
  if (select count(*) from public.crm_tasks where lead_id = l5) = 1 and t like 'Inventario: verificar C2-PRUEBA00001%'
     and t like '%Dato pendiente: estacionamiento por confirmar%'
     and (select assigned_to from public.crm_tasks where lead_id = l5) = v_adm then
    ok := ok + 1; rep := rep || E'\nOK   estacionamiento «Por confirmar»: 1 tarea «requiere verificación» (no coincidencia confirmada), asignada a su asesor';
  else fail := fail + 1; rep := rep || E'\nFAIL requiere verificación: ' || coalesce(t,'—'); end if;

  -- US$ 130 000 × 3.45 = S/ 448 500 ≥ 440 000: coincide con conversión aproximada (440 000 / 3.45 = US$ 127 536; margen 3 % => verificar)
  select string_agg(title || ' | ' || detail, ' // ') into t from public.crm_tasks where lead_id = l6;
  if t like '%conversión aproximada a S/ 3.45 por US$%' and t like '%precio original del inventario%' then
    ok := ok + 1; rep := rep || E'\nOK   búsqueda en dólares: muestra precio original en soles y conversión aproximada: ' || split_part(t, ' | ', 1);
  else fail := fail + 1; rep := rep || E'\nFAIL moneda: ' || coalesce(t,'—'); end if;

  -- Fallo de importación: una carga que falla a mitad no deja cambios ni alertas
  begin
    update public.inventory_properties set price_amount = 300000 where public_reference = 'C2-PRUEBA00002';
    update public.inventory_properties set lifecycle_state = 'active_confirmed' where public_reference = 'C2-PRUEBA00002';
    raise exception 'falla simulada de la importación';
  exception when others then null;
  end;
  perform public.crm_search_cross(false, false, 'prueba');
  select count(*) into n from public.crm_tasks where detail like '%C2-PRUEBA00002%';
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   importación fallida: se deshace y no genera alertas';
  else fail := fail + 1; rep := rep || E'\nFAIL importación fallida generó ' || n || ' tareas'; end if;

  -- Carga incompleta (caen las unidades vigentes): el cruce se omite y no retira coincidencias
  select last_run_units into base_units from public.crm_search_settings where id = 1;
  update public.crm_search_settings set last_run_units = base_units * 10 where id = 1;
  perform public.crm_search_cross(false, false, 'prueba');
  select status || ': ' || detail into t from public.crm_search_runs order by id desc limit 1;
  if t like 'omitido: Posible carga incompleta%' then ok := ok + 1; rep := rep || E'\nOK   guarda de carga incompleta: ' || t;
  else fail := fail + 1; rep := rep || E'\nFAIL guarda de carga incompleta: ' || coalesce(t,'—'); end if;
  update public.crm_search_settings set last_run_units = base_units where id = 1;

  -- Reactivación: la unidad sale del inventario y vuelve => una nueva alerta (cambio relevante)
  update public.inventory_properties set lifecycle_state = 'inactive' where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  update public.inventory_properties set lifecycle_state = 'active_confirmed' where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  perform public.crm_search_cross(false, false, 'prueba');
  select count(*) into n from public.crm_tasks where lead_id = l1;
  if n = 2 and exists (select 1 from public.crm_tasks where lead_id = l1 and detail like '%La unidad vuelve a estar disponible%') then
    ok := ok + 1; rep := rep || E'\nOK   reactivación: una (y solo una) alerta nueva «vuelve a estar disponible»';
  else fail := fail + 1; rep := rep || E'\nFAIL reactivación: ' || n || ' tareas'; end if;

  -- El cliente descarta la unidad: su búsqueda sigue activa y esa unidad no vuelve a avisar
  select id into m1 from public.crm_search_matches where search_id = s1 and ref = 'C2-PRUEBA00001';
  update public.crm_search_matches set outcome = 'descartada_cliente' where id = m1;
  update public.inventory_properties set lifecycle_state = 'inactive' where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  update public.inventory_properties set lifecycle_state = 'active_confirmed' where public_reference = 'C2-PRUEBA00001';
  perform public.crm_search_cross(false, false, 'prueba');
  select count(*) into n from public.crm_tasks where lead_id = l1;
  if n = 2 and (select status from public.crm_active_searches where id = s1) = 'activa' then
    ok := ok + 1; rep := rep || E'\nOK   unidad descartada por el cliente: búsqueda sigue activa y no se repite la alerta';
  else fail := fail + 1; rep := rep || E'\nFAIL descarte: ' || n || ' tareas'; end if;

  select count(*) into n from public.crm_search_match_events where match_id = m1;
  rep := rep || E'\n     historial de la coincidencia: ' || (select string_agg(event, ' → ' order by id) from public.crm_search_match_events where match_id = m1);

  -- Apagado: con enabled = false no se crean tareas
  update public.crm_search_settings set enabled = false where id = 1;
  update public.inventory_properties set lifecycle_state = 'active_confirmed', price_amount = 420000 where public_reference = 'C2-PRUEBA00002';
  perform public.crm_search_cross(false, false, 'prueba');
  select count(*) into n from public.crm_tasks where detail like '%C2-PRUEBA00002%';
  if n = 0 then ok := ok + 1; rep := rep || E'\nOK   interruptor apagado: sin tareas';
  else fail := fail + 1; rep := rep || E'\nFAIL apagado generó tareas'; end if;

  raise exception 'RESULTADO DE PRUEBAS: % OK, % FALLAS (todo se deshizo)%', ok, fail, rep;
end $$;
