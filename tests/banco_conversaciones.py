"""Banco de conversaciones difíciles: negativas, quejas de repetición, cambios de idea, "no, pero…",
cierres y jerga peruana.

Cada caso parte de una conversación ya avanzada (la última pregunta del bot), recibe un mensaje del
cliente y pasa por el mismo camino que el webhook: normalización del texto, observe_conversation (con
OpenAI simulado) y plan_turn (respuesta, etapa, criterios y fotos). Nada sale a la red.

Los casos marcados real=True salen de conversation_messages del teléfono ...3549 del 01/11/2026; su
campo "ia" es la extracción que devolvió gpt-4.1-mini ese día.

hoy_falla (texto) describe lo que el bot todavía hace mal en ese caso; la prueba se registra como fallo
esperado hasta que la etapa 2 (CONVERSACION_IA=on) lo corrija.
"""
import difflib
import json
import re
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest import mock

import app

TELEFONO = "51900003549"

# ---------------------------------------------------------------- inventario simulado

def unidad(ref, proyecto, numero, distrito, precio, dorm, banos, area, piso, entrega, delivery):
    descripcion = f"Proyecto {proyecto}; unidad {numero}. Piso {piso}."
    if delivery == "futura":
        descripcion += f" Entrega según fuente: {entrega}."
    public = {"public_reference": ref, "operation": "compra", "property_type": "departamento", "district": distrito,
              "zone": None, "public_location_reference": None, "price_amount": precio, "currency": "PEN",
              "bedrooms": dorm, "bathrooms": banos, "area_m2": area, "area_total_m2": area, "parking_spaces": None,
              "provenance": "own", "public_description": descripcion}
    return {"property": {"property_id": "id-" + ref}, "public": public, "delivery": delivery}


LIBERTADOR = unidad("C2-268EAF11F560", "Libertador", "502", "Pueblo Libre", 289460, 2, 2, 56.4, 5, "Dic 2027", "futura")
BOLIVAR_1 = unidad("C2-95A5052AC4A0", "TORRE BOLIVAR", "301", "Pueblo Libre", 357000, 2, 2, 48.66, 3, "1er Tri 2029", "futura")
BOLIVAR_2 = unidad("C2-3135000102E8", "TORRE BOLIVAR", "402", "Pueblo Libre", 360000, 2, 2, 49.1, 4, "1er Tri 2029", "futura")
HERRERA = unidad("C2-6EA4E7D3C456", "Casa Herrera", "201", "Pueblo Libre", 360000, 2, 2, 50.9, 2, "Oct 2027", "futura")
BOLIVAR_3D = unidad("C2-B0L1VAR3D001", "TORRE BOLIVAR", "801", "Pueblo Libre", 398000, 3, 2, 68.2, 8, "1er Tri 2029", "futura")
UNIVERSO = unidad("C2-D5ACE8F77103", "Universo", "604", "Chorrillos", 295000, 3, 2, 72.9, 6, None, "inmediata")
SANTO_CRISTO = unidad("C2-8230F7D88B3A", "Santo Cristo", "305", "Surco", 420000, 3, 2, 84.0, 3, None, "inmediata")
NOVA_SURCO = unidad("C2-5URC0A2D0001", "Nova Surco", "1102", "Surco", 380000, 2, 2, 61.0, 11, "Jun 2028", "futura")
JESUS_MARIA = unidad("C2-J35U5MAR1A01", "Parque Salaverry", "703", "Jesús María", 375000, 2, 2, 58.3, 7, "Mar 2028", "futura")
INVENTARIO = [LIBERTADOR, BOLIVAR_1, BOLIVAR_2, HERRERA, BOLIVAR_3D, UNIVERSO, SANTO_CRISTO, NOVA_SURCO, JESUS_MARIA]
POR_REFERENCIA = {item["public"]["public_reference"]: item for item in INVENTARIO}
FOTOS = {ref: [f"https://fotos.test/{ref}/{i}.jpg" for i in range(1, 16)] for ref in POR_REFERENCIA}


def tarjeta(item):
    """Tarjeta de proyecto de prueba con la estructura y el tamaño de las reales (~2.100 caracteres de media),
    incluidos sus propios precios y metrajes, que nunca deben llegar al cliente desde la IA."""
    p = item["public"]
    proyecto = re.match(r"Proyecto (.*?);", p["public_description"]).group(1)
    return (
        "# _INFORMACIÓN DEL PROYECTO_\n\n🔴**EMPRESA DESARROLLADORA**\n\n- Constructora de prueba\n\n"
        "🔴**NÚMERO DE CONTACTO DE VISITAS.**\n\n- Contacto Interno 999888777\n\n"
        "🔴**COMENTARIOS DEL PROYECTO..**\n\n- En construcción. **Precio de entrada:** Desde **S/ 381,200**.\n\n"
        "🔍 **DATOS DEL EDIFICIO:**\n🏢 Antigüedad: De estreno\n🏢 Edificio de 12 pisos\n"
        f"📍**DIRECCIÓN**: Jr. Los Pinos 450, {p['district']}\n\n"
        "📍**UBICACIÓN MAPS**: [https://maps.app.goo.gl/prueba](https://maps.app.goo.gl/prueba)\n\n"
        f"### 📍 Entorno y Conectividad\n\nLa zona de {proyecto} destaca por su tranquilidad y cercanía a todo lo necesario:\n\n"
        "- Rodeado de **parques** (el principal referente es el Parque El Carmen).\n"
        "- Acceso rápido a **bancos y entidades financieras**, mercados y supermercados.\n"
        "- Cercanía a **hospitales y centros de salud**, colegios y universidades.\n"
        "- A pocas cuadras de avenidas principales con transporte público.\n\n"
        "### 🛋️ Áreas Comunes (Amenities)\n\nEl edificio está diseñado con espacios funcionales para el día a día:\n\n"
        "1. **Recepción:** Ingreso controlado y elegante.\n2. **Zona de BBQ:** Espacio al aire libre para parrillas.\n"
        "3. **SUM (Sala de Usos Múltiples):** Para eventos o celebraciones privadas.\n"
        "4. **Zona de bicicletas:** Pensado en la movilidad sostenible del distrito.\n"
        "5. **Gimnasio equipado** y terraza con zona de juegos para niños.\n\n"
        "### 🏢 Características de los Departamentos\n\n"
        "Los departamentos van desde formatos medianos para parejas hasta opciones amplias para familias:\n\n"
        "- **Metraje:** Desde 56 m² hasta 119.66 m².\n- **Dormitorios:** Opciones de 2, 3 y 4 habitaciones.\n"
        "- Acabados de primera, cocina abierta con muebles altos y bajos, pisos laminados en dormitorios.\n"
        "- Ventanas con sistema acústico, calentador eléctrico y puntos de gas natural.\n\n"
        "#### Distribución por Tipologías:\n\n- **2 Dormitorios:** Tipo 2 (56 m²), Tipo 3 (57.3 m²), Tipo 3C (56.4 m²)\n"
        "- **3 Dormitorios:** Tipo 5 (90.7 m²)\n- **4 Dormitorios:** Tipo 1A (98 m²), Tipo 4 (119.66 m²)\n\n"
        "### 📑 Financiamiento\n\nCrédito hipotecario con bancos aliados, Bono Mi Vivienda Verde y uso del fondo AFP. "
        "Separación con 2,000 soles y cuota inicial desde el 10%. Entrega con conformidad de obra e independización.\n"
    )


class InventarioFalso:
    """Mismo contrato que app.InventoryStore, en memoria."""

    def __init__(self, fotos_enviadas=()):
        self.enviadas = set(fotos_enviadas)
        self.consultas = 0

    def find_matches(self, criteria, now=None):
        self.consultas += 1
        distritos = {app.canonical_district(d) or d for d in criteria.get("districts", [])}
        tope = app.budget_in_currency(criteria, "PEN")
        encontrados = [item for item in INVENTARIO
                       if (criteria.get("any_district") or item["public"]["district"] in distritos) and item["public"]["price_amount"] <= tope
                       and criteria.get("bedrooms") in (None, item["public"]["bedrooms"])]
        return app.MatchList(encontrados), now or datetime.now(timezone.utc)

    def project_card(self, reference):
        # En producción cada proyecto tiene su tarjeta de Trello; con ella el bot consulta a la IA sobre la unidad.
        item = POR_REFERENCIA.get(reference)
        return tarjeta(item) if item else ""

    def unit_notice(self, reference):
        return ""

    def photos_for_reference(self, reference, limit=5):
        return FOTOS.get(reference, [])[:limit]

    def closing_question(self, reference):
        if POR_REFERENCIA[reference]["delivery"] == "inmediata":
            return "¿Quieres que coordinemos una visita al departamento?"
        return "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"

    def sent_media(self, phone, urls):
        return {url for url in urls if url in self.enviadas}

    def record_media_delivery(self, phone, search_key, url):
        self.enviadas.add(url)


# ---------------------------------------------------------------- OpenAI simulado

def ia(intent="confirmation", respuesta="", acciones=(), cambio=False, handoff=False, siguiente="reply", pregunta=None, **slots):
    """Observación con la forma exacta del esquema; los slots no indicados van vacíos.
    respuesta_cliente (solo existe con CONVERSACION_IA=on) se agrega desde CLASES al final del archivo."""
    vacios = {"operation": None, "districts": [], "budget_max": None, "currency": None, "bedrooms": None,
              "property_type": None, "parking_required": None, "preferences": [], "payment_method": None,
              "credit_status": None, "purchase_timeline": None, "area_min_m2": None, "bathrooms_min": None}
    return {"intent": intent, "slot_updates": {**vacios, **slots}, "criteria_change": cambio,
            "criteria_actions": [dict(a) for a in acciones], "user_question": pregunta, "next_action": siguiente,
            "handoff": handoff, "assistant_reply": respuesta}


def _ficha_simulada(payload):
    """Lo que devuelve el modelo al redactar la ficha: solo datos de la unidad, con la pregunta de cierre pedida."""
    datos = json.loads(re.search(r"Datos de la unidad: (\{.*?\})\n", payload["input"]).group(1))
    cierre = re.search(r"Termina con una sola pregunta de cierre: '(.*?)'", payload["instructions"]).group(1)
    proyecto = re.match(r"Proyecto (.*?);", datos["public_description"]).group(1)
    return (f"🏠 DEPARTAMENTO EN VENTA – {proyecto.upper()}, {datos['district'].upper()}\n"
            f"💰 PRECIO DE VENTA: S/ {datos['price_amount']:,.0f}\n"
            f"🏡 {datos['bedrooms']} dormitorios · {datos['bathrooms']} baños · {datos['area_total_m2']:g} m²\n\n{cierre}")


def _respuesta_sobre_unidad(payload):
    """Comportamiento real observado el 01/11: ante cualquier mensaje, el modelo vuelve a describir la unidad."""
    datos = json.loads(re.search(r"Datos de la unidad: (\{.*?\})\n", payload["input"]).group(1))
    return (f"Este departamento en {datos['district']} tiene {datos['bedrooms']} dormitorios, {datos['bathrooms']} baños, "
            f"{datos['area_total_m2']:g} m². Precio: S/ {datos['price_amount']:,.0f}. "
            "¿Quieres saber sobre la ubicación o áreas comunes?")


class OpenAIFalso:
    def __init__(self, observacion):
        self.observacion = observacion
        self.llamadas = []

    def __call__(self, payload):
        self.llamadas.append(payload)
        formato = (payload.get("text") or {}).get("format") or {}
        if formato.get("name") == "arenz_conversation_observation":
            texto = json.dumps(self.observacion, ensure_ascii=False)
        elif "Redacta la ficha" in payload["instructions"]:
            texto = _ficha_simulada(payload)
        elif "Responde SOLO lo que el cliente pregunta" in payload["instructions"]:
            texto = _respuesta_sobre_unidad(payload)
        else:
            texto = ""
        respuesta = mock.Mock(status_code=200)
        respuesta.raise_for_status.return_value = None
        respuesta.json.return_value = {"model": "gpt-4.1-mini-2025-04-14", "output_text": texto}
        return respuesta


# ---------------------------------------------------------------- historiales

CRITERIOS_PL = {"operation": "compra", "property_type": "departamento", "districts": ["Pueblo Libre"], "budget_max": 400000,
                "currency": "PEN", "bedrooms": 2, "payment_method": "contado", "purchase_timeline": "lo antes posible",
                "delivery_preference": "futura"}
CRITERIOS_CHORRILLOS = {"operation": "compra", "property_type": "departamento", "districts": ["Chorrillos"], "budget_max": 300000,
                        "currency": "PEN", "bedrooms": 3, "payment_method": "contado", "purchase_timeline": "lo antes posible",
                        "delivery_preference": "inmediata"}
CRITERIOS_SURCO = {"operation": "compra", "property_type": "departamento", "districts": ["Surco", "Chorrillos"],
                   "budget_max": 500000, "currency": "PEN", "bedrooms": 3, "payment_method": "contado"}

LISTADO_PL = ("Con lo que me contaste (departamento, en Pueblo Libre, hasta S/ 400,000, 2 dormitorios) tengo 105 con entrega "
              "entre 2027 y 2029. Te muestro las de entrega en los próximos años:\n\n"
              "1. Libertador · Pueblo Libre\nS/ 289,460 · 2 dorm · 56.4 m² · entrega Dic 2027 · Ref. C2-268EAF11F560\n\n"
              "2. TORRE BOLIVAR · Pueblo Libre\nS/ 357,000 · 2 dorm · 48.66 m² · entrega 1er Tri 2029 · Ref. C2-95A5052AC4A0\n\n"
              "3. TORRE BOLIVAR · Pueblo Libre\nS/ 360,000 · 2 dorm · 49.1 m² · entrega 1er Tri 2029 · Ref. C2-3135000102E8\n\n"
              "Responde con el número de la opción que te interesa y te envío la ficha completa y fotografías del proyecto.")
MAS_OPCIONES_PL = ("Aquí tienes 3 opciones más:\n\n"
                   "2. TORRE BOLIVAR · Pueblo Libre\nS/ 357,000 · 2 dorm · 48.66 m² · entrega 1er Tri 2029 · Ref. C2-95A5052AC4A0\n\n"
                   "3. TORRE BOLIVAR · Pueblo Libre\nS/ 360,000 · 2 dorm · 49.1 m² · entrega 1er Tri 2029 · Ref. C2-3135000102E8\n\n"
                   "4. Casa Herrera · Pueblo Libre\nS/ 360,000 · 2 dorm · 50.9 m² · entrega Oct 2027 · Ref. C2-6EA4E7D3C456\n\n"
                   "Me quedan 26 opciones más; escribe 'más' para verlas. Responde con el número de la opción que te interesa y "
                   "te envío la ficha completa y fotografías del proyecto.")
FICHA_LIBERTADOR = ("🏠 DEPARTAMENTO EN VENTA – PROYECTO LIBERTADOR, PUEBLO LIBRE\n💰 PRECIO DE VENTA: S/ 289,460\n"
                    "🏡 2 dormitorios · 2 baños · 56.4 m²\nRef. C2-268EAF11F560\n\n"
                    "📷 Tengo 14 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-268EAF11F560")
MAS_FOTOS_LIBERTADOR = "📷 Tengo 4 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-268EAF11F560"
MAS_FOTOS_9_LIBERTADOR = "📷 Tengo 9 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-268EAF11F560"
BROCHURE_LIBERTADOR = ("Esas son todas las fotos que tengo de este proyecto. ¿Quieres que un asesor te envíe los planos y el "
                       "brochure del proyecto?\nRef. C2-268EAF11F560")
DESCRIPCION_1841 = ("Este departamento en Pueblo Libre, unidad 502, piso 5, tiene 2 dormitorios, 2 baños y 56.4 m² con vista "
                    "externa. Entrega está prevista para diciembre 2027 y no se confirma si incluye estacionamiento. ¿Quieres "
                    "saber sobre la ubicación o amenities?")
DESCRIPCION_1842 = ("Este departamento de 2 dormitorios y 2 baños tiene 56.4 m², está en el piso 5 con vista externa y entrega "
                    "prevista para dic 2027. ¿Quieres que te detalle las áreas comunes del edificio?")
DESCRIPCION_1850 = ("Este departamento en Pueblo Libre tiene 2 dormitorios, 2 baños, 56.4 m² y está en el piso 5 con vista "
                    "externa. Precio: S/ 289,460. Entrega en diciembre 2027. ¿Quieres saber sobre la ubicación o áreas comunes?")
VISITA_UNIVERSO = "Perfecto. ¿Quieres que coordinemos una visita al departamento?\nRef. C2-D5ACE8F77103"
MAS_FOTOS_UNIVERSO = "📷 Tengo 4 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-D5ACE8F77103"
VISITA_REGISTRADA = ("Perfecto. Registré tu solicitud de visita para la referencia C2-D5ACE8F77103; un asesor de C2 "
                     "Inmobiliaria coordinará contigo.")
SIN_FOTOS_ASESOR = ("Por ahora no tengo las fotos cargadas en el chat. Un asesor de C2 Inmobiliaria puede enviarte las fotos "
                    "de la Ref. C2-268EAF11F560. ¿Quieres que te contacte un asesor?")
PREGUNTA_FUTURA_PL = ("Con lo que me contaste (departamento, en Pueblo Libre, hasta S/ 400,000, 2 dormitorios) tengo 105 con "
                      "entrega entre 2027 y 2029. Por ahora no tengo de entrega inmediata con esos criterios. ¿Te muestro las "
                      "de entrega en los próximos años?")
PREGUNTA_TIEMPO = ("Perfecto, gracias. Último dato antes de mostrarte las opciones: ¿para cuándo necesitas el departamento? "
                   "Por ejemplo: lo antes posible, en los próximos meses o sin apuro.")
PREGUNTA_PAGO = ("Tengo opciones que podrían encajar con lo que buscas. Antes de mostrártelas, ¿cómo piensas pagarlo: al "
                 "contado, con crédito hipotecario o aún lo estás evaluando?")


def historial(etapa, criterios, *mensajes):
    """mensajes alterna cliente/bot y termina en el bot; se guardan solo los 4 últimos turnos, como en producción."""
    turnos = []
    direccion = "user" if len(mensajes) % 2 == 0 else "assistant"
    for contenido in mensajes:
        turnos.append({"direction": direccion, "content": contenido})
        direccion = "assistant" if direccion == "user" else "user"
    return {"stage": etapa, "state": {"active_search_id": "busqueda-1",
                                      "searches": {"busqueda-1": {"criteria": dict(criterios), "recent_turns": turnos[-4:]}}}}


def fotos_enviadas(ref, cuantas):
    return FOTOS[ref][:cuantas]


# ---------------------------------------------------------------- ejecución de un turno

class Resultado:
    def __init__(self, turno, llamadas):
        plan = turno["plan"]
        partes = [plan["text"]] if plan["text"] else []
        partes += [foto["caption"] for foto in plan["photos"] if foto.get("caption")]
        self.visible = app.customer_visible_reply("\n".join(partes))
        self.guardado = plan["stored"]
        self.fotos = plan["photos"]
        self.etapa = turno["stage"]
        self.criterios = turno["criteria"]
        self.llamadas = llamadas

    def __repr__(self):
        return f"<etapa={self.etapa} fotos={len(self.fotos)} visible={self.visible[:160]!r}>"


def ejecutar(caso):
    """Un turno completo, igual que receive_webhook pero sin persistencia ni envío."""
    previous = caso["historia"]
    inventario = InventarioFalso(caso.get("enviadas", ()))
    openai = OpenAIFalso(caso["ia"])
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(app, "OPENAI_API_KEY", "clave-de-prueba"))
        stack.enter_context(mock.patch.object(app, "post_openai_response", openai))
        stack.enter_context(mock.patch.object(app, "get_inventory_store", return_value=inventario))
        stack.enter_context(mock.patch.object(app, "get_conversation_memory_store", return_value=None))
        text = app.resume_answer_as_command(previous, app.normalize_client_text(caso["cliente"]))
        observation = app.observe_conversation(TELEFONO, text, "FALLBACK", {}, previous, True, raw_text=caso["cliente"])
        turno = app.plan_turn(TELEFONO, previous, text, observation, "FALLBACK", inventario)
    return Resultado(turno, openai.llamadas)


# ---------------------------------------------------------------- expectativas (generales, no textos exactos)

def _datos_de(item):
    p = item["public"]
    piso = re.search(r"Piso (\d+)", p["public_description"])
    return [f"{p['price_amount']:,.0f}", f"{p['area_total_m2']:g} m", f"{p['bedrooms']} dormitorios", f"{p['bathrooms']} baños",
            f"piso {piso.group(1)}" if piso else "\0"]


def repite_ficha(r, item):
    texto = r.visible.casefold()
    return sum(dato.casefold() in texto for dato in _datos_de(item)) >= 2


def muestra_listado(r):
    return "responde con el número de la opción" in r.visible.casefold()


def es_ficha(r, ref):
    return "DEPARTAMENTO EN VENTA" in r.visible and ref in r.guardado


def cierra(r):
    return r.etapa in ("awaiting_customer", "do_not_contact")


def precios_y_areas_validos(r, caso):
    """La IA nunca pone precios ni áreas: todo monto o área visible debe existir en el inventario o en los criterios."""
    montos = {int(item["public"]["price_amount"]) for item in INVENTARIO}
    for criterios in (r.criterios, app.conversation_state(caso["historia"])):
        if criterios.get("budget_max"):
            montos.add(int(criterios["budget_max"]))
    areas = {f"{item['public']['area_total_m2']:g}" for item in INVENTARIO}
    for crudo in re.findall(r"(?:S/|US\$)\s?([\d.,]+)", r.visible):
        valor = int(re.sub(r"[.,]\d{1,2}$", "", crudo).replace(",", "").replace(".", ""))
        if valor not in montos:
            return False
    for area in re.findall(r"(\d+(?:[.,]\d+)?)\s?m²", r.visible):
        if area.replace(",", ".") not in areas:
            return False
    return True


# Cada expectativa: (descripción, función(resultado, caso) -> bool)
def E(descripcion, funcion):
    return (descripcion, funcion)


NO_REPITE_LIBERTADOR = E("no vuelve a describir la unidad", lambda r, c: not repite_ficha(r, LIBERTADOR))
def _sin_numeros(texto):
    return re.sub(r"\d[\d.,]*", "#", app.customer_visible_reply(texto)).strip()


def ultimo_mensaje_bot(caso):
    turnos = app.recent_conversation_turns(caso["historia"], limit=8, raw=True)
    return next((t["content"] for t in reversed(turnos) if t["direction"] == "assistant"), "")


def repite_mensaje(r, caso):
    anterior = _sin_numeros(ultimo_mensaje_bot(caso))
    return difflib.SequenceMatcher(None, anterior, _sin_numeros(r.visible)).ratio() > 0.8


NO_REPITE_MENSAJE = E("no repite su último mensaje", lambda r, c: not repite_mensaje(r, c))
NO_LISTADO = E("no muestra un listado de opciones", lambda r, c: not muestra_listado(r))
LISTADO = E("muestra un listado de opciones", lambda r, c: muestra_listado(r))
CIERRA = E("cierra la conversación sin seguimiento", lambda r, c: cierra(r))
NO_CIERRA = E("no cierra la conversación", lambda r, c: not cierra(r))
DERIVA = E("deriva a un asesor", lambda r, c: r.etapa == "handoff")
NO_DERIVA = E("no deriva a un asesor", lambda r, c: r.etapa != "handoff")
FOTOS_SI = E("envía fotos", lambda r, c: bool(r.fotos))
FOTOS_NO = E("no envía fotos", lambda r, c: not r.fotos)
PREGUNTA_CIERRE = E("pasa a la pregunta de cierre de la ficha", lambda r, c: "planos y el brochure" in r.visible or "coordinemos una visita" in r.visible)
SIN_RESUMEN = E("no reinicia el resumen de la búsqueda", lambda r, c: "Con lo que me contaste" not in r.visible)


def criterio(campo, valor):
    return E(f"criterio {campo} = {valor!r}", lambda r, c: r.criterios.get(campo) == valor)


def contiene(texto):
    return E(f"menciona {texto!r}", lambda r, c: texto.casefold() in r.visible.casefold())


def no_contiene(texto):
    return E(f"no menciona {texto!r}", lambda r, c: texto.casefold() not in r.visible.casefold())


def ficha(ref):
    return E(f"envía la ficha de {ref}", lambda r, c: es_ficha(r, ref))


# ---------------------------------------------------------------- casos

PROPUESTA_LIBERTADOR = app.price_proposal_reply(LIBERTADOR) + "\nRef. C2-268EAF11F560"
H_BROCHURE = historial("property_detail", CRITERIOS_PL, "Sí", MAS_FOTOS_LIBERTADOR, "Sí", BROCHURE_LIBERTADOR)
H_MAS_FOTOS = historial("property_detail", CRITERIOS_PL, FICHA_LIBERTADOR, "Sí", MAS_FOTOS_9_LIBERTADOR, "Sí", MAS_FOTOS_LIBERTADOR)
H_FICHA = historial("property_detail", CRITERIOS_PL, LISTADO_PL, "1", FICHA_LIBERTADOR)
H_DESCRIPCION_1841 = historial("property_detail", CRITERIOS_PL, "Sí", BROCHURE_LIBERTADOR, "No", DESCRIPCION_1841)
H_DESCRIPCION_1842 = historial("property_detail", CRITERIOS_PL, "No", DESCRIPCION_1841, "Pero eso ya me lo dijiste en la ficha", DESCRIPCION_1842)
H_DESCRIPCION_1850 = historial("property_detail", CRITERIOS_PL, "Sí", MAS_FOTOS_LIBERTADOR, "No", DESCRIPCION_1850)
H_LISTADO = historial("qualified", CRITERIOS_PL, PREGUNTA_FUTURA_PL, "Sí", LISTADO_PL)
H_MAS_OPCIONES = historial("property_detail", CRITERIOS_PL, "No", DESCRIPCION_1850, "Otra vez?", MAS_OPCIONES_PL)
H_PREGUNTA_FUTURA = historial("qualified", {**CRITERIOS_PL, "delivery_preference": "inmediata"}, PREGUNTA_TIEMPO, "Lo antes posible", PREGUNTA_FUTURA_PL)
H_VISITA = historial("property_detail", CRITERIOS_CHORRILLOS, "Sí", MAS_FOTOS_UNIVERSO, "No", VISITA_UNIVERSO)
H_VISITA_FOTOS_PENDIENTES = historial("property_detail", CRITERIOS_CHORRILLOS, "1", "🏠 DEPARTAMENTO EN VENTA – UNIVERSO, CHORRILLOS\n💰 PRECIO DE VENTA: S/ 295,000\n\n¿Quieres que coordinemos una visita al departamento?\nRef. C2-D5ACE8F77103")
H_MAS_FOTOS_UNIVERSO = historial("property_detail", CRITERIOS_CHORRILLOS, "Sí", "📷 Tengo 9 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-D5ACE8F77103", "Sí", MAS_FOTOS_UNIVERSO)
H_VISITA_REGISTRADA = historial("handoff", CRITERIOS_CHORRILLOS, "No", VISITA_UNIVERSO, "Sí", VISITA_REGISTRADA)
H_CERRADO = historial("awaiting_customer", CRITERIOS_PL, "No", app.OFFER_DECLINED_REPLY.format(brand=app.COMMERCIAL_BRAND))
H_SIN_FOTOS = historial("property_detail", CRITERIOS_PL, "1", FICHA_LIBERTADOR, "Envíame fotos", SIN_FOTOS_ASESOR)
H_DISTRITOS = historial("qualification", {}, "nueva búsqueda", "Nueva búsqueda iniciada. ¿En qué distrito o distritos te gustaría tu departamento?")
H_PAGO = historial("qualification", {k: v for k, v in CRITERIOS_SURCO.items() if k != "payment_method"}, "3", PREGUNTA_PAGO)
H_TIEMPO = historial("qualified", CRITERIOS_SURCO, "Al montado", PREGUNTA_TIEMPO)

TODAS_LIB = fotos_enviadas("C2-268EAF11F560", 15)
ONCE_LIB = fotos_enviadas("C2-268EAF11F560", 11)
TODAS_UNI = fotos_enviadas("C2-D5ACE8F77103", 15)

CASOS = [
    # ---- reales del 01/11/2026 (teléfono ...3549)
    dict(id="real_1841_no_al_brochure", grupo="real", real=True, historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="No",
         ia=ia(respuesta="Entiendo, no enviaré planos ni brochures. ¿Quieres que te ayude con otra consulta o búsqueda?"),
         espera=[CIERRA, NO_REPITE_LIBERTADOR]),
    dict(id="real_1841_ya_me_lo_dijiste_en_la_ficha", grupo="real", real=True, historia=H_DESCRIPCION_1841, enviadas=TODAS_LIB,
         cliente="Pero eso ya me lo dijiste en la ficha",
         ia=ia("general_question", "Disculpa, ¿quieres que te ayude con otra propiedad o información diferente sobre tu búsqueda?",
               pregunta="Ya me diste esa info en la ficha"),
         espera=[NO_REPITE_LIBERTADOR, NO_LISTADO, SIN_RESUMEN], hoy_falla="la IA de unidad vuelve a describir el departamento"),
    dict(id="real_1842_ya_lo_indicaste", grupo="real", real=True, historia=H_DESCRIPCION_1842, enviadas=TODAS_LIB, cliente="Ya lo indicaste",
         ia=ia(respuesta="Entiendo, no te repetiré más detalles. ¿Quieres que te ayude con otra consulta o con la búsqueda de otro departamento?"),
         espera=[NO_REPITE_LIBERTADOR, NO_LISTADO, SIN_RESUMEN], hoy_falla="reinicia el resumen 'Con lo que me contaste…'"),
    dict(id="real_1850_no_a_mas_fotos", grupo="real", real=True, historia=H_MAS_FOTOS, enviadas=ONCE_LIB, cliente="No",
         ia=ia(respuesta="Entiendo, no enviaré más fotos. ¿Quieres que te ayude con otra consulta o filtro para tu búsqueda?"),
         espera=[PREGUNTA_CIERRE, NO_REPITE_LIBERTADOR, FOTOS_NO]),
    dict(id="real_1851_otra_vez", grupo="real", real=True, historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="Otra vez?",
         ia=ia(respuesta="¿Quieres que te vuelva a mostrar los detalles del departamento en Pueblo Libre o prefieres otra consulta?",
               pregunta="Otra vez?", **{k: v for k, v in CRITERIOS_PL.items() if k != "delivery_preference"}),
         espera=[NO_LISTADO, NO_REPITE_LIBERTADOR], hoy_falla="'otra' activa 'más opciones' y lista 3 unidades nuevas"),
    dict(id="real_1840_si_a_mas_fotos", grupo="real", real=True, historia=historial("property_detail", CRITERIOS_PL, LISTADO_PL, "1", FICHA_LIBERTADOR),
         enviadas=fotos_enviadas("C2-268EAF11F560", 1), cliente="Sí", ia=ia(), espera=[FOTOS_SI, NO_CIERRA]),
    dict(id="real_0001_surko_chrrills", grupo="real", real=True, historia=H_DISTRITOS, cliente="Surko, chrrills, once",
         ia=ia("property_search", "Perfecto, buscas departamento en Surco, Chorrillos o San Juan de Lurigancho. ¿Cuál es tu presupuesto y moneda, por favor?",
               [{"field": "districts", "action": "ADD", "values": ["Surco", "Chorrillos", "San Juan de Lurigancho"]}], True,
               siguiente="ask_clarification", operation="compra", property_type="departamento", districts=["Surco", "Chorrillos", "San Juan de Lurigancho"]),
         espera=[E("registra Surco y Chorrillos", lambda r, c: {"Surco", "Chorrillos"} <= set(r.criterios.get("districts", []))),
                 contiene("presupuesto")]),
    dict(id="real_0002_al_montado", grupo="real", real=True, historia=H_PAGO, cliente="Al montado",
         ia=ia("property_search", "Perfecto, pago al contado registrado. ¿Quieres que te muestre opciones de departamentos con 3 dormitorios hasta 500,000 soles?",
               [{"field": "payment_method", "action": "UPDATE", "values": ["contado"]}], payment_method="contado"),
         espera=[criterio("payment_method", "contado"), contiene("para cuándo")]),
    dict(id="real_0002_al_toque_toke", grupo="real", real=True, historia=H_TIEMPO, cliente="Al toque toke",
         ia=ia("confirmation", "Gracias por compartir cuándo necesitas el depa. Reviso opciones en Surco, Chorrillos o Once con 3 cuartos y hasta 500k soles.",
               [{"field": "purchase_timeline", "action": "UPDATE", "values": ["al toque toke"]}], True, siguiente="search_inventory",
               purchase_timeline="al toque toke"),
         espera=[LISTADO, contiene("entrega inmediata"), no_contiene("¿Te muestro las de entrega inmediata o")]),
    dict(id="real_1933_no_a_mas_fotos_entrega_inmediata", grupo="real", real=True, historia=H_MAS_FOTOS_UNIVERSO, enviadas=fotos_enviadas("C2-D5ACE8F77103", 11),
         cliente="No", ia=ia(respuesta="Entiendo, si quieres que te ayude con otra búsqueda o tienes alguna consulta, estoy aquí para ayudarte."),
         espera=[contiene("coordinemos una visita"), FOTOS_NO]),
    dict(id="real_1933_si_a_la_visita", grupo="real", real=True, historia=H_VISITA, enviadas=TODAS_UNI, cliente="Sí", ia=ia("human_handoff", handoff=True),
         espera=[DERIVA, contiene("visita")]),

    # ---- negativas
    dict(id="neg_nel_al_brochure", grupo="negativa", historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="nel",
         ia=ia(respuesta="Entendido, no te enviaré el brochure."), espera=[CIERRA, NO_REPITE_LIBERTADOR]),
    dict(id="neg_no_gracias_asi_esta_bien_a_la_visita", grupo="negativa", historia=H_VISITA, enviadas=TODAS_UNI, cliente="no gracias, así está bien",
         ia=ia(respuesta="Entendido, no coordinaré la visita."), espera=[CIERRA, NO_DERIVA]),
    dict(id="neg_nop_a_mas_fotos", grupo="negativa", historia=H_MAS_FOTOS, enviadas=ONCE_LIB, cliente="nop",
         ia=ia(respuesta="Entendido, no enviaré más fotos."), espera=[PREGUNTA_CIERRE, FOTOS_NO, NO_REPITE_LIBERTADOR]),
    dict(id="neg_no_a_ver_entrega_futura", grupo="negativa", historia=H_PREGUNTA_FUTURA, cliente="No",
         ia=ia(respuesta="Entendido. ¿Prefieres ampliar los distritos o revisar el presupuesto?"),
         espera=[NO_LISTADO, NO_REPITE_MENSAJE], hoy_falla="repite la misma pregunta de entrega"),
    dict(id="neg_ninguna_me_convence", grupo="negativa", historia=H_LISTADO, cliente="no, ninguna me convence",
         ia=ia("general_question", "Entiendo. ¿Quieres que ajustemos el distrito, el presupuesto o los dormitorios?"),
         espera=[NO_REPITE_MENSAJE, E("no envía una ficha", lambda r, c: "DEPARTAMENTO EN VENTA" not in r.visible), NO_CIERRA], hoy_falla="vuelve a mostrar el mismo listado"),
    dict(id="neg_todavia_no_al_asesor", grupo="negativa", historia=H_SIN_FOTOS, cliente="todavía no",
         ia=ia(respuesta="Entendido, no te contactará un asesor por ahora."), espera=[CIERRA, NO_DERIVA, NO_REPITE_LIBERTADOR]),

    # ---- quejas de repetición
    dict(id="rep_ya_me_lo_dijiste", grupo="repeticion", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="ya me lo dijiste",
         ia=ia(respuesta="Disculpa la repetición. ¿Quieres ver otra opción o que un asesor te contacte?"),
         espera=[NO_REPITE_LIBERTADOR, NO_LISTADO, SIN_RESUMEN], hoy_falla="vuelve a describir el departamento"),
    dict(id="rep_otra_vez_lo_mismo", grupo="repeticion", historia=H_DESCRIPCION_1841, enviadas=TODAS_LIB, cliente="otra vez lo mismo?",
         ia=ia(respuesta="Disculpa. ¿Quieres revisar otra opción o prefieres que un asesor te contacte?"),
         espera=[NO_LISTADO, NO_REPITE_LIBERTADOR], hoy_falla="'otra' activa 'más opciones'"),
    dict(id="rep_me_estas_repitiendo", grupo="repeticion", historia=H_DESCRIPCION_1842, enviadas=TODAS_LIB, cliente="me estás repitiendo lo mismo",
         ia=ia("general_question", "Tienes razón, disculpa. ¿Te muestro otra opción o te contacta un asesor?"),
         espera=[NO_REPITE_LIBERTADOR, NO_LISTADO, SIN_RESUMEN], hoy_falla="reinicia el resumen y lista opciones"),
    dict(id="rep_otra_vez_tras_mas_opciones", grupo="repeticion", historia=H_MAS_OPCIONES, cliente="Otra vez?",
         ia=ia(respuesta="Disculpa, ¿quieres revisar alguna de estas opciones?"),
         espera=[NO_LISTADO, NO_REPITE_MENSAJE], hoy_falla="muestra todavía más opciones"),
    dict(id="rep_y_dale_con_lo_mismo", grupo="repeticion", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="y dale con lo mismo",
         ia=ia(respuesta="Disculpa la repetición. ¿Quieres ver otra opción?"),
         espera=[NO_REPITE_LIBERTADOR, NO_LISTADO], hoy_falla="vuelve a describir el departamento"),

    dict(id="rep_ya_te_dije_con_dato", grupo="repeticion", cliente="ya te dije, en Surco",
         historia=historial("qualification", {}, "Hola", "Nueva búsqueda iniciada. ¿En qué distrito o distritos te gustaría tu departamento?"),
         ia=ia("property_search", "Disculpa. Anoto Surco.", [{"field": "districts", "action": "ADD", "values": ["Surco"]}], True, districts=["Surco"]),
         espera=[criterio("districts", ["Surco"]), contiene("presupuesto")]),

    # ---- cambios de idea
    dict(id="cambio_mejor_en_surco", grupo="cambio", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="mejor en Surco",
         ia=ia("change_criteria", "Perfecto, cambio la búsqueda a Surco.", [{"field": "districts", "action": "UPDATE", "values": ["Surco"]}], True,
               districts=["Surco"]),
         espera=[criterio("districts", ["Surco"]), NO_REPITE_LIBERTADOR, contiene("Nova Surco")]),
    dict(id="cambio_mejor_3_cuartos", grupo="cambio", historia=H_LISTADO, cliente="ya no quiero 2 cuartos, mejor 3",
         ia=ia("change_criteria", "Perfecto, busco con 3 dormitorios.", [{"field": "bedrooms", "action": "UPDATE", "values": [3]}], True, bedrooms=3),
         espera=[criterio("bedrooms", 3), NO_REPITE_MENSAJE, contiene("3 dorm")]),
    dict(id="cambio_no_mejor_la_2", grupo="cambio", historia=H_FICHA, enviadas=fotos_enviadas("C2-268EAF11F560", 1), cliente="no, mejor la 2",
         ia=ia(respuesta="Claro, te muestro la opción 2."), espera=[ficha("C2-95A5052AC4A0")], hoy_falla="toma el 'no' como 'no más fotos' y no envía la opción 2"),
    dict(id="cambio_presupuesto_en_dolares", grupo="cambio", historia=H_LISTADO, cliente="mejor hasta 120 mil dólares",
         ia=ia("change_criteria", "Perfecto, ajusto el presupuesto.", [{"field": "budget_max", "action": "UPDATE", "values": [120000]},
                                                                       {"field": "currency", "action": "UPDATE", "values": ["USD"]}], True,
               budget_max=120000, currency="USD"),
         espera=[criterio("budget_max", 120000), criterio("currency", "USD"), LISTADO]),

    # ---- "no, pero…"
    dict(id="no_pero_muestrame_otra", grupo="no_pero", historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="no, pero muéstrame otra",
         ia=ia("property_search", "Claro, te muestro otras opciones."), espera=[NO_CIERRA, LISTADO, NO_DERIVA]),
    dict(id="no_pero_mandame_mas_fotos", grupo="no_pero", historia=H_VISITA_FOTOS_PENDIENTES, enviadas=fotos_enviadas("C2-D5ACE8F77103", 1),
         cliente="no, pero mándame más fotos", ia=ia("general_question", "Claro, te envío más fotos."),
         espera=[NO_CIERRA, FOTOS_SI, NO_DERIVA]),
    dict(id="no_pero_tienes_en_surco", grupo="no_pero", historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="no, pero tienes en Surco?",
         ia=ia("change_criteria", "Reviso opciones en Surco.", [{"field": "districts", "action": "UPDATE", "values": ["Surco"]}], True, districts=["Surco"]),
         espera=[NO_CIERRA, criterio("districts", ["Surco"]), contiene("Nova Surco")]),
    dict(id="no_pero_tiene_cochera", grupo="no_pero", historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="no gracias, pero ¿tiene cochera?",
         ia=ia("general_question", "Te confirmo sobre el estacionamiento.", pregunta="¿tiene cochera?"),
         espera=[NO_CIERRA, contiene("estacionamiento"), NO_DERIVA]),

    # ---- cierres
    dict(id="cierre_ok_gracias_tras_cerrar", grupo="cierre", historia=H_CERRADO, cliente="ok gracias", ia=ia(respuesta="¡Con gusto!"),
         espera=[E("despedida corta", lambda r, c: r.visible == app.CLOSING_THANKS_REPLY), NO_DERIVA], hoy_falla="el normalizador convierte 'ok gracias' en 'Sí' y se vuelve a listar"),
    dict(id="cierre_listo_gracias_por_todo", grupo="cierre", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="listo, gracias por todo",
         ia=ia(respuesta="¡Gracias a ti! Aquí estaré si necesitas algo más."), espera=[CIERRA, NO_REPITE_LIBERTADOR], hoy_falla="vuelve a describir el departamento en vez de despedirse"),
    dict(id="cierre_lo_voy_a_pensar", grupo="cierre", historia=H_BROCHURE, enviadas=TODAS_LIB, cliente="gracias, lo voy a pensar",
         ia=ia(respuesta="Claro, tómate tu tiempo."), espera=[CIERRA, NO_DERIVA], hoy_falla="vuelve a describir el departamento"),
    dict(id="cierre_ya_fue_no_me_interesa", grupo="cierre", historia=H_LISTADO, cliente="ya fue, no me interesa",
         ia=ia(respuesta="Entendido, gracias por escribir."), espera=[E("no volver a contactar", lambda r, c: r.etapa == "do_not_contact")]),
    dict(id="cierre_chau_a_la_visita", grupo="cierre", historia=H_VISITA, enviadas=TODAS_UNI, cliente="chau",
         ia=ia(respuesta="¡Hasta luego!"), espera=[CIERRA, NO_DERIVA], hoy_falla="vuelve a describir el departamento"),

    # ---- jerga peruana
    dict(id="jerga_ya_pe_mandame", grupo="jerga", historia=H_MAS_FOTOS, enviadas=ONCE_LIB, cliente="ya pe, mándame", ia=ia(),
         espera=[FOTOS_SI, NO_CIERRA]),
    dict(id="jerga_dale_nomas_a_la_visita", grupo="jerga", historia=H_VISITA, enviadas=TODAS_UNI, cliente="dale nomás", ia=ia(),
         espera=[DERIVA], hoy_falla="'dale nomás' no se reconoce como sí"),
    dict(id="jerga_simon_a_entrega_futura", grupo="jerga", historia=H_PREGUNTA_FUTURA, cliente="simón", ia=ia(siguiente="search_inventory"),
         espera=[LISTADO]),
    dict(id="jerga_chevere_la_2", grupo="jerga", historia=H_LISTADO, cliente="chévere, la 2", ia=ia(), espera=[ficha("C2-95A5052AC4A0")]),
    # ---- preguntas sobre la unidad y seguridad de los datos
    dict(id="dato_cuanto_cuesta", grupo="dato", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="y cuánto cuesta al final?",
         ia=ia("general_question", "El precio es de S/ 289,460.", pregunta="¿cuánto cuesta?"),
         espera=[contiene("289,460"), NO_CIERRA]),
    dict(id="dato_donde_queda", grupo="dato", historia=H_MAS_FOTOS, enviadas=ONCE_LIB, cliente="y dónde queda exactamente?",
         ia=ia("general_question", "Queda en Jr. Los Pinos 450, Pueblo Libre, cerca del Parque El Carmen.", pregunta="¿dónde queda?"),
         espera=[contiene("Jr. Los Pinos"), NO_REPITE_LIBERTADOR], hoy_falla="la IA de unidad vuelve a describir el departamento en vez de dar la dirección"),
    dict(id="seguridad_ia_inventa_precio", grupo="dato", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="ya me lo dijiste",
         ia=ia(respuesta="Disculpa, este depa cuesta S/ 250,000 y tiene 60 m², está disponible."),
         espera=[no_contiene("250,000"), no_contiene("disponible"), NO_REPITE_LIBERTADOR],
         hoy_falla="vuelve a describir el departamento"),
    # ---- negociación de precio (02/11: la contraoferta se tomó como presupuesto nuevo)
    dict(id="negociacion_real_a_ese_precio", grupo="negociacion", real=True, historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB,
         cliente="Tengo 200 mil soles en mano, puedes venderme a ese precio",
         ia=ia("change_criteria", None, [{"field": "budget_max", "action": "UPDATE", "values": [200000]}], True, budget_max=200000, currency="PEN"),
         espera=[criterio("budget_max", 400000), contiene("289,460"), contiene("propuesta"), NO_LISTADO, NO_CIERRA],
         hoy_falla="cambia el presupuesto a S/ 200,000 y vuelve a buscar"),
    dict(id="negociacion_me_lo_dejas", grupo="negociacion", historia=H_MAS_FOTOS, enviadas=ONCE_LIB, cliente="¿me lo dejas en 250 mil?",
         ia=ia("general_question", "Entiendo, consulto con un asesor."),
         espera=[criterio("budget_max", 400000), contiene("propuesta"), NO_REPITE_LIBERTADOR],
         hoy_falla="la IA de unidad vuelve a describir el departamento"),
    dict(id="negociacion_si_al_asesor", grupo="negociacion", cliente="Sí", ia=ia(),
         historia=historial("property_detail", CRITERIOS_PL, "Tengo 200 mil, ¿me lo vendes?", PROPUESTA_LIBERTADOR),
         espera=[DERIVA, contiene("interés")]),
    dict(id="negociacion_no_al_asesor", grupo="negociacion", cliente="no, gracias", ia=ia(),
         historia=historial("property_detail", CRITERIOS_PL, "Tengo 200 mil, ¿me lo vendes?", PROPUESTA_LIBERTADOR),
         espera=[CIERRA, NO_DERIVA]),

    dict(id="jerga_ta_caro", grupo="jerga", historia=H_DESCRIPCION_1850, enviadas=TODAS_LIB, cliente="ta caro",
         ia=ia("general_question", "Entiendo. ¿Quieres que busque opciones con un presupuesto menor?"),
         espera=[NO_REPITE_LIBERTADOR, NO_CIERRA], hoy_falla="vuelve a describir el departamento en vez de ofrecer opciones más baratas"),
]


# Clasificación que devolvería la IA con CONVERSACION_IA=on: (tipo, pedido, opción, dato).
# Con el interruptor apagado el campo no existe en el esquema y el código lo ignora.
CLASES = {
    "real_1841_no_al_brochure": ("rechaza",),
    "real_1841_ya_me_lo_dijiste_en_la_ficha": ("queja_repeticion",),
    "real_1842_ya_lo_indicaste": ("queja_repeticion",),
    "real_1850_no_a_mas_fotos": ("rechaza",),
    "real_1851_otra_vez": ("queja_repeticion",),
    "real_1840_si_a_mas_fotos": ("acepta",),
    "real_1933_no_a_mas_fotos_entrega_inmediata": ("rechaza",),
    "real_1933_si_a_la_visita": ("acepta",),
    "neg_nel_al_brochure": ("rechaza",),
    "neg_no_gracias_asi_esta_bien_a_la_visita": ("rechaza",),
    "neg_nop_a_mas_fotos": ("rechaza",),
    "neg_no_a_ver_entrega_futura": ("rechaza",),
    "neg_ninguna_me_convence": ("rechaza",),
    "neg_todavia_no_al_asesor": ("rechaza",),
    "rep_ya_me_lo_dijiste": ("queja_repeticion",),
    "rep_otra_vez_lo_mismo": ("queja_repeticion",),
    "rep_me_estas_repitiendo": ("queja_repeticion",),
    "rep_otra_vez_tras_mas_opciones": ("queja_repeticion",),
    "rep_y_dale_con_lo_mismo": ("queja_repeticion",),
    "rep_ya_te_dije_con_dato": ("queja_repeticion",),
    "cambio_mejor_en_surco": ("pide_otra_cosa", "cambiar_criterios"),
    "cambio_mejor_3_cuartos": ("pide_otra_cosa", "cambiar_criterios"),
    "cambio_no_mejor_la_2": ("pide_otra_cosa", "opcion", 2),
    "cambio_presupuesto_en_dolares": ("pide_otra_cosa", "cambiar_criterios"),
    "no_pero_muestrame_otra": ("pide_otra_cosa", "otras_opciones"),
    "no_pero_mandame_mas_fotos": ("pide_otra_cosa", "fotos"),
    "no_pero_tienes_en_surco": ("pide_otra_cosa", "cambiar_criterios"),
    "no_pero_tiene_cochera": ("pide_otra_cosa", "dato_unidad", None, "estacionamiento"),
    "cierre_ok_gracias_tras_cerrar": ("cierra",),
    "cierre_listo_gracias_por_todo": ("cierra",),
    "cierre_lo_voy_a_pensar": ("cierra",),
    "cierre_ya_fue_no_me_interesa": ("cierra",),
    "cierre_chau_a_la_visita": ("cierra",),
    "jerga_ya_pe_mandame": ("acepta",),
    "jerga_dale_nomas_a_la_visita": ("acepta",),
    "jerga_simon_a_entrega_futura": ("acepta",),
    "jerga_chevere_la_2": ("pide_otra_cosa", "opcion", 2),
    "jerga_ta_caro": ("pide_otra_cosa", "cambiar_criterios"),
    "dato_cuanto_cuesta": ("pide_otra_cosa", "dato_unidad", None, "precio"),
    "dato_donde_queda": ("pide_otra_cosa", "dato_unidad", None, "ubicacion"),
    "seguridad_ia_inventa_precio": ("queja_repeticion",),
    "negociacion_real_a_ese_precio": ("otra",),  # lo que devolvió la IA real: la regla de respaldo lo detecta
    "negociacion_me_lo_dejas": ("pide_otra_cosa", "negociar_precio"),
    "negociacion_si_al_asesor": ("acepta",),
    "negociacion_no_al_asesor": ("rechaza",),
}


def clasificacion(tipo="otra", pedido="ninguno", opcion=None, dato="ninguno"):
    return {"tipo": tipo, "pedido": pedido, "opcion": opcion, "dato": dato}


for _caso in CASOS:
    _caso["ia"]["respuesta_cliente"] = clasificacion(*CLASES.get(_caso["id"], ()))
