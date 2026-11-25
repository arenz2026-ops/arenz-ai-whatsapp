-- 25/11: la foto de Trello de vd022 San Miguel era el número de cuenta de la empresa y llegó a un cliente (prueba …3549).
-- Las 16 imágenes cargadas desde Trello el 12/11 quedan sin aprobar hasta que alguien las revise a ojo.
update inventory_project_media set approved_for_client = false where storage_path ~ '_trello_\d+\.jpg$';
select public.sync_property_media_from_projects();

-- Para volver a aprobar una imagen revisada:
-- update inventory_project_media set approved_for_client = true where storage_path = 'proyectos/<slug>/<slug>_trello_01.jpg';
-- select public.sync_property_media_from_projects();
