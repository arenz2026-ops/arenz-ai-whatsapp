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

## Fotos de inmuebles

El bot envía las fotos como imágenes de WhatsApp, siempre antes del texto:

- Al presentar opciones: la foto principal de cada una (máximo 3).
- Al elegir una unidad o pedir "fotos": hasta 5 fotos de esa unidad; si pide más, recibe
  las siguientes. Con varias opciones en pantalla puede decir "fotos de la 2"; si no
  indica cuál, el bot le pregunta.
- Si la unidad no tiene fotos (o ya se enviaron todas), lo dice y ofrece que un asesor se
  las envíe. Si el cliente acepta, se registra la derivación y se avisa al asesor con la
  referencia. Una respuesta de derivación nunca se reemplaza por fotos.
- Los pies de foto no muestran la referencia ARZ. Si una foto falla, el texto sale igual.
- No se repite una foto al mismo cliente dentro de la misma búsqueda (tabla
  `media_deliveries`; sin Supabase se recuerda en la memoria del proceso).

Solo se envían filas de `inventory_property_media` con `media_type = 'photo'`,
`approved_for_client = true` y enlace `https://`.

Ejecuta una vez en el SQL Editor de Supabase:

```sql
-- Qué fotos recibió cada cliente en cada búsqueda, para no repetirlas.
create table if not exists public.media_deliveries (
  id bigint generated always as identity primary key,
  phone text not null,
  search_key text not null,
  media_url text not null,
  sent_at timestamptz not null default now(),
  unique (phone, search_key, media_url)
);
alter table public.media_deliveries enable row level security;
revoke all on public.media_deliveries from anon, authenticated;

-- Bucket público de las fotos: solo JPG de hasta 1 MB.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('inmuebles', 'inmuebles', true, 1048576, array['image/jpeg'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;
```

El bucket es público porque WhatsApp descarga cada foto desde su enlace: quien tenga el
enlace puede verla, incluso antes de aprobarla. Los nombres llevan un código derivado del
archivo, así que no se pueden adivinar, y el bucket no se puede listar sin la clave de
servidor.

### Subir fotos con `media_import.py`

Organiza las fotos en una carpeta por referencia. El orden de los nombres es el orden de
envío y la primera es la foto principal. Formatos: JPG, PNG o WEBP (las HEIC del iPhone
se exportan antes como JPG).

```
fotos/
  ARZ-000001/
    01-sala.jpg
    02-cocina.png
```

```powershell
$env:SUPABASE_URL = "https://TU-PROYECTO.supabase.co"
$env:SUPABASE_KEY = "clave-de-servidor"
python media_import.py fotos                          # prueba: muestra el plan, no escribe nada
python media_import.py fotos --apply                  # sube y registra sin aprobar
python media_import.py fotos --apply --approve-media  # aprueba las fotos de esa carpeta
```

Cada foto se convierte a JPG de máximo 1600 px y 1 MB, sin metadatos (incluida la
ubicación GPS), se sube a `inmuebles/<referencia>/` y se registra en
`inventory_property_media` con `approved_for_client = false`. Revisa las fotos en los
enlaces que imprime `--apply` y apruébalas con `--approve-media` (también se puede usar
desde el inicio para subir y aprobar a la vez). Repetir el comando con la misma carpeta
no duplica nada. Si alguna foto se rechaza (formato, archivo dañado o referencia
inexistente), `--apply` no hace nada hasta corregirla.

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
