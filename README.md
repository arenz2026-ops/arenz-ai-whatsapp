# ARENZ AI WhatsApp MVP

Flask para: WhatsApp Cloud API → `/webhook` → IA con fallback determinista →
respuesta Graph API → lead SQLite.

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
| `LEADS_DB_PATH` | SQLite; en Render usa disco persistente, p. ej. `/var/data/arenz_leads.db`. |
| `PORT` | Render lo configura automáticamente. |

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
2. Crea y monta un disco persistente en `/var/data`; sin él SQLite no sobrevive a reinicios.
3. Carga las variables secretas en el panel de Render, nunca en Git.
4. Despliega y verifica `https://TU-SERVICIO.onrender.com/health` devuelve `{"status":"ok"}`.

## Meta: número de prueba

1. En Meta Developers → WhatsApp → Configuration, usa `https://TU-SERVICIO.onrender.com/webhook` como Callback URL.
2. Ingresa exactamente `VERIFY_TOKEN` como Verify token y suscribe `messages`.
3. En App settings → Basic, carga App Secret solo en `APP_SECRET` de Render.
4. En WhatsApp → API Setup, carga token temporal y Phone number ID en Render, y agrega el teléfono destinatario de prueba.
5. Envía `hola`: debe recibirse respuesta y un lead en `LEADS_DB_PATH`.
