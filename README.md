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
| `CONVERSACION_IA` | Opcional; `on` u `off` (por defecto `off`). Con `on`, la misma llamada a OpenAI clasifica cómo respondió el cliente a la última pregunta del bot (acepta, rechaza, pide otra cosa, se queja de repetición, cierra u otra) y el código actúa según eso; las preguntas sobre la unidad se responden en esa misma llamada. Precios, áreas y disponibilidad salen siempre del inventario. En `off` el bot funciona como antes. Antes de encenderlo: `OPENAI_API_KEY=... python scripts/probar_ia_real.py`. |
| `SUPABASE_URL` | URL del proyecto Supabase para persistencia durable en Render. |
| `SUPABASE_KEY` | Clave de servidor Supabase; nunca la expongas en el cliente o Git. |
| `LEADS_DB_PATH` | SQLite solo como fallback local. |
| `PORT` | Render lo configura automáticamente. |
| `ADVISOR_PHONE` | Opcional. Números de asesor que reciben el aviso de cada derivación, separados por coma (`51979300318,51996353549`; se acepta `979300318`). |
| `ADVISOR_TEMPLATE` | Opcional. Nombre de la plantilla aprobada para el aviso; sin ella se envía texto libre. |
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
24 h. Para que el aviso llegue siempre, crea en Meta una plantilla de categoría
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
4. Se revisa el resultado en el buscador web y con una conversación de prueba.
