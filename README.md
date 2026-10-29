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

## Fotos de inmuebles

El bot envía fotos como mensajes de imagen de WhatsApp (por enlace HTTPS), siempre
antes del texto:

- Al presentar opciones: la foto principal de cada una (máximo 3).
- Al elegir una unidad o pedir fotos ("fotos de la 2", o solo "fotos" si hay una
  unidad en pantalla): hasta 5 fotos de esa unidad.
- Si se piden fotos con varias opciones en pantalla, pregunta de cuál.
- Si la unidad no tiene fotos aprobadas (o ya se enviaron todas), lo dice y ofrece
  que un asesor se las envíe; un "sí" registra la derivación y avisa al asesor.
- Una respuesta de derivación nunca se reemplaza ni lleva fotos.

Los pies de foto describen la unidad sin la referencia interna. Solo se envían las
filas de `inventory_property_media` con `approved_for_client = true`. La misma foto
no se repite al mismo cliente dentro de una búsqueda (`media_deliveries`; si la
tabla falla, el bot recuerda los envíos en memoria hasta el siguiente reinicio). Si
una foto falla, se registra en el log y el texto se envía igual.

Ejecuta una vez en el SQL Editor de Supabase:

```sql
create table if not exists public.media_deliveries (
  id bigint generated always as identity primary key,
  phone text not null,
  search_key text not null,
  media_url text not null,
  delivered_at timestamptz not null default now(),
  unique (phone, search_key, media_url)
);
alter table public.media_deliveries enable row level security;
revoke all on public.media_deliveries from anon, authenticated;

-- Bucket público: WhatsApp descarga cada foto desde su URL pública.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('inmuebles', 'inmuebles', true, 1048576, array['image/jpeg'])
on conflict (id) do update
  set public = true,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;
```

### Subir fotos (`media_import.py`)

Organiza una carpeta con una subcarpeta por `public_reference`; el orden alfabético
de los archivos es el orden de las fotos y la primera es la principal:

```
fotos/
  C2-00003A4AD688/
    01-fachada.jpg
    02-sala.png
```

```powershell
$env:SUPABASE_URL = "https://TU-PROYECTO.supabase.co"
$env:SUPABASE_KEY = "clave-service-role"
python media_import.py fotos                                # prueba: no sube ni escribe nada
python media_import.py fotos --apply                        # sube y registra sin aprobar
python media_import.py fotos --apply --approve-media        # tras revisarlas: aprobadas para clientes
```

Cada foto se convierte a JPG de máximo 1 MB y 1600 px por lado, sin metadatos EXIF
(las fotos de celular traen la ubicación GPS), se sube a `inmuebles/REFERENCIA/` y se
registra en `inventory_property_media` con `approved_for_client = false`; el bot no
la envía hasta aprobarla con `--approve-media`. Sin `--apply` solo muestra el plan
(con las credenciales, además verifica que cada referencia exista). Volver a correrlo
es seguro: una foto ya registrada no se vuelve a subir, y el script nunca borra. Con
cualquier error (referencia inexistente, archivo ilegible) no aplica nada.
