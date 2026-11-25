# ARENZ AI WhatsApp MVP

Flask para: WhatsApp Cloud API → `/webhook` → IA con fallback determinista →
respuesta Graph API → lead durable en Supabase (o SQLite local como fallback).

## Variables de entorno

Configúralas en Render o localmente, nunca en Git.

| Variable | Uso |
| --- | --- |
| `VERIFY_TOKEN` | Secreto propio para la verificación Meta. |
| `WHATSAPP_TOKEN` | Token del número de prueba Cloud API. |
| `PHONE_NUMBER_ID` | ID del número de prueba Meta. |
| `APP_SECRET` | App Secret Meta para `X-Hub-Signature-256`. |
| `OPENAI_API_KEY` | Clave de servidor para IA; si falta, usa fallback. |
| `OPENAI_MODEL` | Opcional; por defecto `gpt-4.1-mini`. |
| `CONVERSACION_IA` | Opcional; `on` u `off` (por defecto `on`; solo `off`, `0`, `false` o `no` lo apagan). Con `on`, la misma llamada a OpenAI clasifica cómo respondió el cliente a la última pregunta del bot (acepta, rechaza, pide otra cosa, se queja de repetición, cierra u otra) y el código actúa según eso; las preguntas sobre la unidad se responden en esa misma llamada. Precios, áreas y disponibilidad salen siempre del inventario. En `off` el bot funciona como antes del 02/11 (queda como interruptor de emergencia). Para probar la clasificación con la API real: `OPENAI_API_KEY=... python scripts/probar_ia_real.py`. |
| `AGENT_COMMISSION_SHARE` | Opcional; por defecto `50 % para cada parte`. Cuando escribe un agente o corredor inmobiliario que busca para un cliente, el bot confirma la colaboración y menciona esta comisión. Vacío: confirma la colaboración sin mencionar comisión. |
| `SEGUIMIENTO_AUTOMATICO` | Opcional; `on` u `off` (por defecto `off`). Con `on`, `scripts/enviar_seguimientos.py` escribe una vez a quien vio un listado o una ficha y no respondió: “Hola Ana, ¿cómo estás? 😊 … ¿pudiste revisar las opciones que te compartí?”. Nunca tras un cierre, un “no me contacten” o una derivación al asesor, y solo dentro de las 24 h desde el último mensaje del cliente (después WhatsApp no permite texto libre). |
| `CRON_TOKEN` | Opcional. Contraseña que GitHub Actions envía a `/tareas/seguimientos`; sin ella el seguimiento programado no corre. |
| `SEGUIMIENTO_HORAS` | Opcional; por defecto `20`. Horas de silencio del cliente antes del seguimiento (debe ser menor que 23,5). |
| `SEGUIMIENTO_DESDE` / `SEGUIMIENTO_HASTA` | Opcional; por defecto `9` y `20`. Horario de Lima en que se envían seguimientos. |
| `SUPABASE_URL` | URL del proyecto Supabase para persistencia durable en Render. |
| `SUPABASE_KEY` | Clave de servidor Supabase; nunca la expongas en el cliente o Git. |
| `LEADS_DB_PATH` | SQLite solo como fallback local. |
| `PORT` | Render lo configura automáticamente. |
| `ADVISOR_PHONE` | Opcional. Números de asesor que reciben el aviso de cada derivación, separados por coma (`51979300318,51996353549`; se acepta `979300318`). |
| `ADVISOR_TEMPLATE` | Opcional. Nombre de la plantilla aprobada para el aviso; sin ella se envía texto libre. |
| `TRELLO_KEY` / `TRELLO_TOKEN` | Clave de API y token **de solo lectura** de la Power-Up "CREENZIA fotos" (cuenta que ve el tablero Intermediación Inmobiliaria). Los usa `/tareas/fotos-trello` para descargar los adjuntos. |
| `ADVISOR_TEMPLATE_LANGUAGE` | Opcional; por defecto `es`. Código de idioma con que se aprobó la plantilla. |

## Local

```powershell
python -m pip install -r requirements.txt
$env:VERIFY_TOKEN = "elige-un-secreto"
$env:WHATSAPP_TOKEN = "token-de-meta"
$env:PHONE_NUMBER_ID = "id-del-numero-de-prueba"
$env:APP_SECRET = "app-secret-de-meta"
$env:OPENAI_API_KEY = "clave-de-openai"
$env:LEADS_DB_PATH = "$PWD\leads.db"
python app.py
python -m unittest discover -s tests -v
```

El banco de conversaciones difíciles (`tests/banco_conversaciones.py`) simula OpenAI y el inventario:
no necesita red. Los casos que el bot aún resuelve mal figuran como fallo esperado (`hoy_falla`).
Para medir los tokens que se envían a OpenAI en cada caso: `python scripts/medir_tokens.py`.

## Render

1. Conecta el repositorio y usa `render.yaml`.
2. Carga las variables secretas en el panel de Render, nunca en Git. Para durabilidad en Render Free configura `SUPABASE_URL` y `SUPABASE_KEY`.
3. Ejecuta una vez, en el SQL Editor de Supabase, el ajuste mínimo mostrado abajo.
4. Despliega y verifica `https://TU-SERVICIO.onrender.com/health` devuelve `{"status":"ok"}`.

## Esquema mínimo de Supabase

```sql
-- La tabla leads existente conserva: phone, intent, district, budget,
-- bedrooms y created_at. Cada interacción inserta un registro nuevo.
alter table public.leads
  add column if not exists conversation jsonb,
  add column if not exists status text,
  add column if not exists next_action text;

create table if not exists public.processed_messages (
  message_id text primary key
);

create table if not exists public.conversation_sessions (
  phone text primary key,
  stage text not null,
  state jsonb not null default '{}'::jsonb,
  summary text,
  updated_at timestamptz not null default now()
);

create table if not exists public.conversation_messages (
  id uuid primary key default gen_random_uuid(),
  phone text not null,
  message_key text unique not null,
  direction text not null check (direction in ('inbound', 'outbound')),
  content text not null,
  extraction jsonb,
  created_at timestamptz not null default now()
);
```

La clave usada por Render debe poder insertar y actualizar esta tabla. No publiques
esa clave ni la uses en una aplicación cliente.

## Meta: número de prueba

1. En Meta Developers → WhatsApp → Configuration, usa `https://TU-SERVICIO.onrender.com/webhook` como Callback URL.
2. Ingresa exactamente `VERIFY_TOKEN` como Verify token y suscribe `messages`.
3. En App settings → Basic, carga App Secret solo en `APP_SECRET` de Render.
4. En WhatsApp → API Setup, carga token temporal y Phone number ID en Render, y agrega el teléfono destinatario de prueba.
5. Envía `hola`: debe recibirse respuesta y un lead en `LEADS_DB_PATH`.

## Coexistencia (Embedded Signup)

`/conectar-whatsapp` conecta el número de la app WhatsApp Business a Cloud API sin
borrar la app. El callback canjea el código, rechaza cualquier WABA que no sea del
portafolio `ALLOWED_BUSINESS_ID` (Arenz por defecto), suscribe la app a los webhooks
y guarda token y `phone_number_id` en Supabase; el bot los usa en lugar de
`WHATSAPP_TOKEN`/`PHONE_NUMBER_ID`. El token caduca a los 60 días: repetir la conexión.

Variables opcionales: `META_APP_ID`, `ES_CONFIG_ID`, `ALLOWED_BUSINESS_ID`.

```sql
create table if not exists public.whatsapp_connection (
  id integer primary key default 1 check (id = 1),
  waba_id text not null,
  phone_number_id text not null,
  access_token text not null,
  business_id text,
  connected_at timestamptz not null default now()
);
alter table public.whatsapp_connection enable row level security;
revoke all on public.whatsapp_connection from anon, authenticated;
```

## Aviso al asesor

Cuando el cliente pide asesor, llamada, visita o muestra interés en un inmueble, el
bot responde al cliente, registra la derivación en Supabase y, al final, envía por
WhatsApp a cada número de `ADVISOR_PHONE` un aviso con el teléfono del cliente
(enlace `wa.me`), el tipo de solicitud, la referencia del inmueble y los filtros
Qué / Dónde / Cuánto / Cómo / Cuándo (lo no declarado figura como "no indicado").
Si el aviso falla, se registra en el log y la conversación sigue normal.

WhatsApp solo acepta texto libre hacia un número que escribió al bot en las últimas
24 h. **Costo 0:** cada asesor escribe cada mañana «hola» al número del bot; el bot le
confirma que la ventana quedó abierta (sin tratarlo como cliente) y las notas de ese día
llegan gratis. Un mensaje más largo de un asesor se atiende como cliente (sirve para pruebas). Para que el aviso llegue siempre, crea en Meta una plantilla de categoría
*Utility* y configura `ADVISOR_TEMPLATE`. El bot llena cuatro parámetros del cuerpo:

```
Nuevo lead para atender.
Cliente: {{1}}
Solicitud: {{2}}
Inmueble: {{3}}
Criterios: {{4}}
```

## Actualización semanal del inventario

Cada semana (la vigencia de una unidad es de 7 días) se renueva todo el inventario; no solo se reconfirman las unidades
existentes, también se **agregan las nuevas** y se retiran las que ya no figuran. Jerarquía de precios cuando una unidad
aparece en varias fuentes: **Lista de Precios C2 > Power BI Tale > Trello**.

1. **Lista de Precios C2** (Looker Studio) y **Power BI Tale**: se leen completas desde el navegador y se envían a
   `inventory_staging` con un token de un solo uso (`inventory_import_tokens`). En SQL se comparan con
   `inventory_properties`: se reconfirman y actualizan precios de las existentes, se **insertan las nuevas**, y las que ya
   no figuran pasan a `inactive`. Cocheras y depósitos no se cargan como departamentos.
2. **Trello** (tablero Intermediación Inmobiliaria): se envían todas las tarjetas abiertas a `inventory_trello_cards`
   (vía `/admin/inventario-receptor`). Toda tarjeta de **Departamentos segundo uso**, **Captación** o **P. Entrega
   inmediata** que no tenga unidad en el inventario se **crea** como unidad (`source_name = trello`), con su antigüedad,
   estacionamiento y nota en `inventory_unit_info`, y se vincula en `inventory_project_cards`. Las tarjetas que pasan a
   **Vendido** o **Descartado** retiran su unidad (`sold_rented` / `inactive`).
3. Se aplican las correcciones fijas de `inventory_overrides` (dormitorios, estacionamiento, vendidos) para que la nueva
   carga no reintroduzca errores de origen.
   - `inventory_unit_info.client_notice = 'carga'` hace que el bot cierre la ficha con un aviso general (sin detalle) de que
     el inmueble tiene una carga y que lo consulte con un asesor. No se borra en la recarga; quitarlo cuando se levante.
4. **Fotos de Trello**: `POST /tareas/fotos-trello` (cabecera `X-Cron-Token`) descarga los adjuntos de imagen de cada
   tarjeta vinculada al inventario (máx. 15, fuera de VENDIDO/DESCARTADO), convierte HEIC/PNG a JPG de máx. 1600 px, los
   sube al bucket `inmuebles` (`proyectos/<slug>/<slug>_trello_NN.jpg`), los registra en `inventory_project_media` y llama a
   `sync_property_media_from_projects()`. Corre en segundo plano; `GET` del mismo enlace muestra el resultado. Es
   idempotente, no toca tarjetas que ya tienen fotos curadas y se omiten adjuntos con nombre de documento (DNI, partida, contrato…). **Revisión visual**: cada imagen
   nueva pasa por un modelo con visión (`OPENAI_API_KEY`, `OPENAI_MODEL`) antes de aprobarse. Solo `foto`, `render` y
   `plano` quedan aprobadas; `no_enviar` (números de cuenta, vouchers, DNI, capturas…) o una revisión fallida quedan con
   `approved_for_client = false` y aparecen en el log como *held for manual review*. Sin clave de OpenAI no se aprueba nada. Se ejecuta cada lunes desde
   GitHub Actions (`.github/workflows/fotos-trello.yml`) y a mano con **Actions → Fotos Trello → Run workflow**.
   - **Fotos por tipología**: `inventory_project_media.bedrooms` (opcional) limita una foto a las unidades con ese número
     de dormitorios; vacío = todas las unidades del proyecto. Las fotos que no están en Trello (p. ej. carpetas de Drive
     privadas) se publican en `static/fotos/<proyecto>/` y se registran con `media_url` del servicio
     (`https://arenz-ai-whatsapp-prod.onrender.com/static/fotos/...`). Primer caso: Mar de Grau (`2d-NN.jpg`, `3d-NN.jpg`).
   - **Categoría**: `inventory_project_media.category` (`foto`, `piloto`, `plano`, `general`, `render`) se copia a cada
     unidad (`inventory_property_media.category`). Por ahora el bot envía todas, renders incluidos, porque la mayoría de
     unidades entregadas aún no tiene fotos reales. Una imagen que no es real debe quedar con `category = 'render'`.
5. Se revisa el resultado en el buscador web y con una conversación de prueba.

## Búsquedas activas con alertas de inventario (CRM CREENZIA)

Cuando un prospecto busca algo que hoy no está en el **Inventario C2**, su búsqueda queda guardada. Si después aparece una
unidad que la cumple, el CRM crea **una tarea interna** para el asesor responsable (módulo Tareas) con la referencia `C2-…`,
el proyecto/unidad, el precio en su moneda original, el motivo y la fecha del inventario. **No se escribe al cliente**: el
asesor reconfirma la unidad y decide. Todo vive en Supabase (`sql/busquedas_activas.sql`); costo 0.

- **Búsqueda**: se arma sola con los criterios que ya capturó el bot (operación, tipo, distritos, dormitorios, presupuesto y
  moneda, forma de pago, entrega, estacionamiento). En la ficha del prospecto → «Búsqueda activa» se edita, se marca qué es
  **obligatorio** y qué es preferencia, y se pausa, cierra o reanuda. Si falta un obligatorio queda «pendiente de confirmación»
  y no se cruza. La forma de pago es informativa (el inventario no la trae).
- **Reglas**: solo unidades `active_confirmed` (fuera reservadas, vendidas, inactivas). Un obligatorio «Por confirmar» en la
  unidad (dormitorios, entrega, estacionamiento) ⇒ **requiere verificación**, nunca coincidencia confirmada. Otra moneda ⇒
  conversión aproximada (S/ 3.45 por US$, igual que el Inventario C2); a ±3 % del límite ⇒ requiere verificación.
- **Cuándo cruza**: un trigger por sentencia en `inventory_properties` solo marca `inventory_changed_at`. `pg_cron`
  (`crm-busquedas-activas`, cada 5 min) cruza cuando pasaron 10 min sin cambios (carga terminada), cuando hay búsquedas nuevas o
  una vez al día. Guardas: no cruza si el inventario está vencido (> 10 días) o si las unidades vigentes caen más de 30 %
  frente al cruce anterior (carga incompleta). Una importación fallida se deshace sola y no deja rastro.
- **Sin duplicados**: una fila por (búsqueda, unidad) y la tarea solo nace cuando la unidad **empieza** a cumplir (nueva,
  vuelve a estar disponible o cambio de precio/entrega/dormitorios). Repetir la carga o reintentar no crea otra tarea. Si la
  unidad sale y vuelve, se avisa una vez más; si el cliente la descartó, no. Lo que ya calza al crear, editar o reanudar una
  búsqueda queda como **línea base** (se ve en la ficha, sin tarea). Historial en `crm_search_match_events`.
- **Suprimidas**: búsquedas pausadas o cerradas, prospectos «No contactar», de prueba o con cierre ganado.

**Pruebas**: `sql/busquedas_activas_pruebas.sql` (datos ficticios sobre la base real; termina con un error a propósito para
deshacer todo y mostrar el informe): caso Lince 2 dorm. entrega inmediata hasta S/ 450 000 con la unidad bajando de
S/ 465 000 a S/ 440 000 procesada 3 veces ⇒ 1 sola tarea para la asesora; reservada, requisito pendiente, cerrada,
No contactar, importación fallida, carga incompleta, reactivación, descarte del cliente y apagado ⇒ sin tareas de más.

**Activar / desactivar** (solo administrador):
1. CRM → **Tareas** → «Alertas de inventario» → **Simular ahora** (no guarda nada) y revisar.
2. **Activar alertas** → **Confirmar activación**. Por defecto lo que ya calza hoy queda como línea base; marcar «Avisar también
   lo que ya calza hoy» solo si se quiere una tarea por cada coincidencia actual.
3. **Apagar alertas** detiene las tareas al instante (las búsquedas y el historial se conservan).
   Por SQL: `update crm_search_settings set enabled = false where id = 1;`
   Simulación por SQL: `select * from crm_search_cross(true);` (en modo simulación no escribe más que el registro en `crm_search_runs`).

**Pendiente de producción**: la actualización semanal del inventario (Lista de Precios C2 y Power BI → `inventory_staging`
→ SQL; Trello) sigue iniciándose a mano; las alertas corren solas apenas termina esa carga.

## Seguimiento automático

Se ejecuta cada hora a **costo 0** con GitHub Actions (`.github/workflows/seguimientos.yml`), que
llama a `POST /tareas/seguimientos` del servicio en Render (despierta el plan gratis si está dormido).
Para activarlo:

1. En Render, variables `CRON_TOKEN` (una contraseña larga inventada) y `SEGUIMIENTO_AUTOMATICO=on`.
2. En GitHub, **Settings → Secrets and variables → Actions → New repository secret**: `CRON_TOKEN`
   con el mismo valor.
3. En **Actions → Seguimientos → Run workflow** se puede probar a mano.

Sin `CRON_TOKEN` el enlace responde 503; con un token distinto, 403; con `SEGUIMIENTO_AUTOMATICO`
apagado no envía nada. `scripts/enviar_seguimientos.py` sigue sirviendo para ejecutarlo a mano.

Antes de encenderlo, `python scripts/enviar_seguimientos.py --simular` muestra qué mensajes enviaría, sin enviarlos ni guardar nada.

## Tokens de OpenAI por mensaje

Cada mensaje entrante guarda en `conversation_messages.extraction._uso_openai` las llamadas que hizo a OpenAI
(tipo `observacion` o `ficha`, modelo, tokens de entrada y salida). Para medir el consumo real en Supabase:

```sql
select count(distinct m.id) mensajes,
       round(avg(t.entrada) filter (where t.tipo = 'observacion')) obs_entrada_prom,
       round(avg(t.salida)  filter (where t.tipo = 'observacion')) obs_salida_prom,
       round(sum(t.total)::numeric / count(distinct m.id)) total_por_mensaje
from conversation_messages m,
     jsonb_to_recordset(m.extraction->'_uso_openai') as t(tipo text, entrada int, salida int, total int)
where m.direction = 'inbound' and m.created_at > now() - interval '7 days';
```
