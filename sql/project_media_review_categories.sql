-- 25/11: categorías de la revisión visual de fotos (trello_photos.ImageReviewer).
-- no_enviar = la revisión la rechazó (cuenta bancaria, voucher, DNI, capturas…); revisar = sin revisión, a mano.
alter table inventory_project_media drop constraint inventory_project_media_category_check;
alter table inventory_project_media add constraint inventory_project_media_category_check
  check (category = any (array['render','piloto','foto','general','plano','no_enviar','revisar']));

-- Revisión a ojo de las 16 imágenes cargadas de Trello el 12/11:
-- 3 no_enviar (flyer de cuentas bancarias del desarrollador ×2, diapositiva interna para agentes), 12 render, 1 plano.
update inventory_project_media set category = 'no_enviar', approved_for_client = false
 where storage_path in ('proyectos/quinones-park/quinones-park_trello_01.jpg',
                        'proyectos/v017-surco-ss04/v017-surco-ss04_trello_01.jpg',
                        'proyectos/vd022-san-miguel/vd022-san-miguel_trello_01.jpg');
update inventory_project_media set category = 'plano', approved_for_client = true, approved_at = now()
 where storage_path = 'proyectos/v017-surco-ss04/v017-surco-ss04_trello_02.jpg';
update inventory_project_media set category = 'render', approved_for_client = true, approved_at = now()
 where storage_path ~ '_trello_\d+\.jpg$' and category not in ('no_enviar', 'plano');
select public.sync_property_media_from_projects();
