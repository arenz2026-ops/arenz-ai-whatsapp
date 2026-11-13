"""Banco de conversaciones difíciles (ver tests/banco_conversaciones.py) e interruptor CONVERSACION_IA.

Cada caso corre dos veces:
- off: el comportamiento anterior. Los casos con hoy_falla quedan como fallo esperado.
- on: la IA clasifica la respuesta del cliente y el código actúa. Todos deben pasar.
"""
import copy
import json
import logging
import os
import unittest
from unittest import mock

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")

import app  # noqa: E402
import banco_conversaciones as banco  # noqa: E402


def ejecutar(caso, modo):
    logging.disable(logging.INFO)
    try:
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": modo}):
            return banco.ejecutar(caso)
    finally:
        logging.disable(logging.NOTSET)


def llamadas_sin_ficha(resultado):
    return [p for p in resultado.llamadas if "Redacta la ficha" not in p["instructions"]]


class InterruptorConversacionIATest(unittest.TestCase):
    def test_encendido_por_defecto(self):
        """Si la variable se borra en Render, el bot no vuelve al comportamiento anterior al 02/11."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONVERSACION_IA", None)
            self.assertTrue(app.conversacion_ia_activa())

    def test_valores_reconocidos(self):
        """Solo un apagado explícito apaga; vacío o mal escrito deja la IA conversacional encendida."""
        for valor, esperado in (("on", True), ("ON", True), (" 1 ", True), ("true", True), ("sí", True),
                                ("", True), ("   ", True), ("onn", True), ("cualquiera", True),
                                ("off", False), ("OFF", False), (" off ", False), ("0", False), ("false", False),
                                ("False", False), ("no", False)):
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": valor}):
                self.assertEqual(app.conversacion_ia_activa(), esperado, repr(valor))

    def test_apagado_ignora_la_clasificacion(self):
        """En off, que la IA traiga o no respuesta_cliente no cambia nada: el comportamiento es el anterior."""
        for caso in banco.CASOS:
            sin_clase = copy.deepcopy(caso)
            del sin_clase["ia"]["respuesta_cliente"]
            con, sin = ejecutar(caso, "off"), ejecutar(sin_clase, "off")
            self.assertEqual((con.guardado, con.etapa, con.criterios, con.fotos),
                             (sin.guardado, sin.etapa, sin.criterios, sin.fotos), caso["id"])

    def test_payload_apagado_sin_cambios(self):
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "off"}):
            payload = app.build_observation_payload(banco.H_BROCHURE, "Sí", raw_text="ok gracias")
        self.assertIs(payload["text"]["format"]["schema"], app.OBSERVATION_SCHEMA)
        self.assertNotIn("respuesta_cliente", payload["instructions"])
        self.assertNotIn("ultima_pregunta_bot", payload["input"])
        self.assertTrue(payload["input"].endswith("Mensaje: Sí"))

    def test_payload_encendido(self):
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso()):
            payload = app.build_observation_payload(banco.H_BROCHURE, "Sí", raw_text="ok gracias")
        self.assertIs(payload["text"]["format"]["schema"], app.OBSERVATION_SCHEMA_IA)
        self.assertIn("respuesta_cliente", payload["instructions"])
        contexto = json.loads(payload["input"].split("\n", 1)[0].removeprefix("Contexto: "))
        self.assertEqual(contexto["ultima_pregunta_bot"], "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?")
        self.assertNotIn("ficha_proyecto", contexto, "un 'ok gracias' se clasifica sin la tarjeta del proyecto")
        self.assertTrue(payload["input"].endswith("Mensaje: ok gracias"), "la IA lee el mensaje tal como lo escribió el cliente")
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso()):
            payload = app.build_observation_payload(banco.H_BROCHURE, "y dónde queda?")
        contexto = json.loads(payload["input"].split("\n", 1)[0].removeprefix("Contexto: "))
        self.assertIn("Jr. Los Pinos", contexto["ficha_proyecto"])

    def test_tarjeta_solo_cuando_hace_falta(self):
        for mensaje in ("Sí", "no", "No gracias", "ok gracias", "chau", "dale"):
            self.assertFalse(app.needs_unit_card(mensaje), mensaje)
        for mensaje in ("y dónde queda?", "no, pero tiene cochera", "qué áreas comunes tiene el edificio", "ya me lo dijiste"):
            self.assertTrue(app.needs_unit_card(mensaje), mensaje)


class TextoDeLaIATest(unittest.TestCase):
    """La IA nunca escribe precios, áreas, unidades ni disponibilidad: su texto se descarta si los trae."""

    def test_descarta_datos_de_inventario(self):
        for texto in ("Cuesta S/ 250,000.", "Son US$ 90 000", "Está en 300 mil soles", "Tiene 60 m² techados",
                      "Mide 72.9 m2", "Quedan unidades disponibles", "La unidad 502 es ideal", "Tiene 3 dormitorios",
                      "Está en el piso 5", "Ref. C2-ABC123", "Te mostraré las opciones luego"):
            self.assertIsNone(app.ai_reply_text({"assistant_reply": texto}, None), texto)

    def test_acepta_texto_sin_datos(self):
        for texto in ("Disculpa la repetición. ¿Quieres ver otra opción?",
                      "Queda en Jr. Los Pinos 450, Pueblo Libre, cerca del Parque El Carmen."):
            self.assertEqual(app.ai_reply_text({"assistant_reply": texto}, None), texto)

    def test_descarta_repetir_el_ultimo_mensaje(self):
        previous = banco.historial("property_detail", banco.CRITERIOS_PL, "No", "¿Quieres saber sobre la ubicación o áreas comunes?")
        self.assertIsNone(app.ai_reply_text({"assistant_reply": "¿Quieres saber sobre la ubicación o las áreas comunes?"}, previous))

    def test_dato_de_la_unidad_sale_del_inventario(self):
        self.assertIn("S/ 289,460", app.unit_fact_reply(banco.LIBERTADOR, "precio"))
        self.assertIn("56.4 m²", app.unit_fact_reply(banco.LIBERTADOR, "area"))
        self.assertIn("Dic 2027", app.unit_fact_reply(banco.LIBERTADOR, "entrega"))
        self.assertIn("entrega inmediata", app.unit_fact_reply(banco.UNIVERSO, "entrega"))
        self.assertIsNone(app.unit_fact_reply(banco.LIBERTADOR, "ubicacion"))


class FallasDeSupabaseTest(unittest.TestCase):
    def test_fotos_o_cierre_no_disponibles_no_rompen_el_turno(self):
        import requests
        for caso_id in ("real_1850_no_a_mas_fotos", "cambio_no_mejor_la_2"):
            caso = next(c for c in banco.CASOS if c["id"] == caso_id)
            with mock.patch.object(banco.InventarioFalso, "closing_question", side_effect=requests.ConnectionError), \
                    mock.patch.object(banco.InventarioFalso, "sent_media", side_effect=requests.ConnectionError):
                resultado = ejecutar(caso, "on")
            self.assertTrue(resultado.visible.strip(), caso_id)


class WebhookCompletoTest(unittest.TestCase):
    """De punta a punta por /webhook: la IA lee el mensaje original y el cliente recibe la despedida corta."""

    def test_ok_gracias_tras_el_cierre(self):
        import hashlib
        import hmac
        caso = next(c for c in banco.CASOS if c["id"] == "cierre_ok_gracias_tras_cerrar")
        memoria = mock.Mock()
        memoria.claim_work.return_value = ("claimed", "token")
        memoria.load_session.return_value = caso["historia"]
        openai = banco.OpenAIFalso(caso["ia"])
        cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-e2e", "from": banco.TELEFONO, "type": "text", "text": {"body": "ok gracias"}}]}}]}]}).encode()
        firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "OPENAI_API_KEY", "clave"), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso()), \
                mock.patch.object(app, "post_openai_response", openai), \
                mock.patch.object(app, "post_graph_message") as graph:
            respuesta = app.app.test_client().post("/webhook", data=cuerpo, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": firma})
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(openai.llamadas[0]["input"].endswith("Mensaje: ok gracias"))
        self.assertEqual(graph.call_args[0][0]["text"]["body"], app.CLOSING_THANKS_REPLY)
        self.assertEqual(memoria.commit_work.call_args[0][0]["p_session"]["stage"], "awaiting_customer")


class InvariantesTest(unittest.TestCase):
    """En los dos modos y en todos los casos, también en los de fallo esperado."""

    def test_invariantes(self):
        for modo in ("off", "on"):
            for caso in banco.CASOS:
                with self.subTest(modo=modo, caso=caso["id"]):
                    resultado = ejecutar(caso, modo)
                    observaciones = [p for p in resultado.llamadas
                                     if (p.get("text") or {}).get("format", {}).get("name") == "arenz_conversation_observation"]
                    self.assertEqual(len(observaciones), 1, "una sola observación de la IA por mensaje")
                    self.assertTrue(banco.precios_y_areas_validos(resultado, caso),
                                    f"precio o área que no viene del inventario: {resultado.visible!r}")
                    self.assertTrue(resultado.visible.strip() or resultado.fotos, "el cliente no recibe nada")
                    if modo == "on":
                        self.assertEqual(len(llamadas_sin_ficha(resultado)), 1,
                                         "con CONVERSACION_IA=on solo la ficha puede hacer una segunda llamada")


class ConversacionDificilTest(unittest.TestCase):
    def revisar(self, caso, modo):
        resultado = ejecutar(caso, modo)
        fallas = [descripcion for descripcion, cumple in caso["espera"] if not cumple(resultado, caso)]
        self.assertEqual(fallas, [], f"{caso['id']} ({modo}): {resultado!r}")


def _crear_prueba(caso, modo):
    def prueba(self):
        self.revisar(caso, modo)
    prueba.__doc__ = f"[{modo}·{caso['grupo']}] {caso['cliente']!r}"
    if modo == "off" and caso.get("hoy_falla"):
        prueba.__doc__ += f" — sin la IA conversacional: {caso['hoy_falla']}"
        return unittest.expectedFailure(prueba)
    return prueba


for _caso in banco.CASOS:
    for _modo in ("off", "on"):
        setattr(ConversacionDificilTest, f"test_{_modo}_{_caso['id']}", _crear_prueba(_caso, _modo))


if __name__ == "__main__":
    unittest.main()


class TarjetaDelProyectoTest(unittest.TestCase):
    """El recorte de la tarjeta conserva los datos públicos del edificio y sigue quitando los internos."""

    TARJETA_INMUEBLE = (
        "# _INFORMACIÓN DEL INMUEBLE_\n\n🔴**TÍTULARES DEL INMUEBLE.**\n\n- Registrado a nombre de Persona Ejemplo\n\n"
        "🔴**NÚMERO DE CONTACTO.**\n\n- Contacto Ejemplo 987654321\n\n🔴**RATIO DE VENTA:**\n\n"
        "- PRECIO AL MERCADO: 285,000 soles\n- PRECIO AL CIERRE: 280,000 soles\n\n"
        "DEPARTAMENTO EN VENTA - SURCO\n\n🔍DATOS DEL EDIFICIO:\n🏢Antigüedad de 10 años.\n🏢 Edificio de 8 pisos\n"
        "📍DIRECCIÓN: Jr. Ejemplo 102, Surco\n🎯 ÁREAS COMUNES:\n👮 Vigilancia 24/7\n💰PRECIO: S/. 285,000 SOLES Negociable\n")

    def test_conserva_antiguedad_y_pisos(self):
        limpio = app.clean_card_text(self.TARJETA_INMUEBLE)
        for dato in ("Antigüedad de 10 años", "Edificio de 8 pisos", "Jr. Ejemplo 102", "Vigilancia"):
            self.assertIn(dato, limpio)

    def test_sigue_quitando_lo_interno(self):
        limpio = app.clean_card_text(self.TARJETA_INMUEBLE)
        for interno in ("Persona Ejemplo", "Contacto Ejemplo", "987654321", "280,000", "285,000", "Negociable"):
            self.assertNotIn(interno, limpio)

    def test_variantes_del_encabezado(self):
        for encabezado in ("🔍 **DATOS DEL EDIFICIO:**", "🔍DATOS DE EDIFICIO:", "🔍 DATOS DEL EDIFICIO:"):
            tarjeta = f"🔴**CONTACTO**\n- Interno\n\n{encabezado}\n🏢 Antigüedad: De estreno\n📍 DIRECCIÓN: Jr. X 1"
            limpio = app.clean_card_text(tarjeta)
            self.assertIn("De estreno", limpio, encabezado)
            self.assertNotIn("Interno", limpio, encabezado)

    def test_sin_bloque_de_edificio_no_cambia(self):
        tarjeta = "🔴**CONTACTO**\n- Interno\n\n📍 DIRECCIÓN: Jr. X 1\nParque cercano"
        self.assertEqual(app.clean_card_text(tarjeta).strip(), "📍 DIRECCIÓN: Jr. X 1\nParque cercano")

    def test_la_ia_recibe_la_antiguedad(self):
        """La pregunta del 02/11 ('¿cuántos años de antigüedad?') ahora llega con el dato en ficha_proyecto."""
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso()):
            payload = app.build_observation_payload(banco.H_MAS_FOTOS, "Cuántos años de antigüedad tiene?")
        contexto = json.loads(payload["input"].split("\n", 1)[0].removeprefix("Contexto: "))
        self.assertIn("Antigüedad: De estreno", contexto["ficha_proyecto"])
        self.assertIn("12 pisos", contexto["ficha_proyecto"])
        self.assertNotIn("999888777", contexto["ficha_proyecto"])


class NegociacionTest(unittest.TestCase):
    def test_respuesta_con_precio_del_inventario(self):
        texto = app.price_proposal_reply(banco.LIBERTADOR)
        self.assertIn("S/ 289,460", texto)
        self.assertIn("te contacte un asesor", texto)

    def test_frases_de_negociacion(self):
        for frase in ("puedes venderme a ese precio", "¿me lo dejas en 250 mil?", "véndemelo en 300", "hay descuento?",
                      "¿es negociable?", "te ofrezco 350 mil", "cuál es el último precio", "¿lo dejas en 380?"):
            self.assertTrue(app.NEGOTIATION_PATTERN.search(frase), frase)
        for frase in ("ta caro", "cuánto cuesta?", "busca opciones más baratas", "mi presupuesto es 300 mil", "dónde queda"):
            self.assertFalse(app.NEGOTIATION_PATTERN.search(frase), frase)


class ContraofertaEnCuantoTest(unittest.TestCase):
    """La contraoferta queda en CUÁNTO (aviso al asesor y ficha del lead); el presupuesto no cambia."""

    def test_lee_el_monto(self):
        casos = {"Tengo 200 mil soles en mano, puedes venderme a ese precio": (200000, "PEN"),
                 "¿me lo dejas en 250 mil?": (250000, "PEN"), "te ofrezco 350k": (350000, "PEN"),
                 "¿lo dejas en 380?": (380000, "PEN"), "te doy 90 mil dólares": (90000, "USD"),
                 "véndemelo en S/ 270,000": (270000, "PEN"), "te ofrezco 260.000 soles": (260000, "PEN")}
        for texto, (monto, moneda) in casos.items():
            self.assertEqual(app.offered_amount(texto, banco.LIBERTADOR), {"amount": monto, "currency": moneda}, texto)
        self.assertIsNone(app.offered_amount("hay descuento?", banco.LIBERTADOR))
        self.assertIsNone(app.offered_amount("me lo dejas más barato, la unidad 2", banco.LIBERTADOR))

    def test_se_guarda_sin_tocar_el_presupuesto(self):
        caso = next(c for c in banco.CASOS if c["id"] == "negociacion_real_a_ese_precio")
        resultado = ejecutar(caso, "on")
        self.assertEqual(resultado.criterios["budget_max"], 400000)
        self.assertEqual(resultado.criterios["price_offer"], {"amount": 200000, "currency": "PEN", "reference": "C2-268EAF11F560"})

    def test_cuanto_en_el_aviso(self):
        import advisor_notify
        criterios = {**banco.CRITERIOS_PL, "price_offer": {"amount": 200000, "currency": "PEN", "reference": "C2-268EAF11F560"}}
        cuanto = dict(advisor_notify.criteria_lines(criterios))["Cuánto"]
        self.assertEqual(cuanto, "Ofrece PEN 200,000 por C2-268EAF11F560")
        sin_monto = {**banco.CRITERIOS_PL, "price_offer": {"amount": None, "currency": None, "reference": "C2-268EAF11F560"}}
        self.assertEqual(dict(advisor_notify.criteria_lines(sin_monto))["Cuánto"], "Pide rebaja por C2-268EAF11F560, sin monto")
        self.assertEqual(dict(advisor_notify.criteria_lines(banco.CRITERIOS_PL))["Cuánto"], "PEN 400,000")

    def test_de_punta_a_punta_la_oferta_llega_al_asesor(self):
        """Turno 1: la contraoferta. Turno 2: 'Sí'. El aviso al asesor trae la oferta en CUÁNTO y la ficha del lead también."""
        import hashlib
        import hmac
        caso = next(c for c in banco.CASOS if c["id"] == "negociacion_real_a_ese_precio")
        memoria = mock.Mock()
        memoria.claim_work.return_value = ("claimed", "token")
        sesion = {"actual": copy.deepcopy(caso["historia"])}
        memoria.load_session.side_effect = lambda telefono: sesion["actual"]

        def guardar(payload):
            # Lo que el RPC de Supabase deja como sesión para el turno siguiente.
            busqueda = payload["p_search"]
            sesion["actual"] = {"stage": payload["p_session"]["stage"],
                                "state": {"active_search_id": busqueda["search_id"],
                                          "searches": {busqueda["search_id"]: busqueda["state"]}}}
            guardados.append(payload)
        guardados = []
        memoria.commit_work.side_effect = guardar

        def enviar(texto, mensaje_id, observacion):
            openai = banco.OpenAIFalso(observacion)
            cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
                {"id": mensaje_id, "from": banco.TELEFONO, "type": "text", "text": {"body": texto}}]}}]}]}).encode()
            firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on", "ADVISOR_PHONE": "51900000001", "ADVISOR_TEMPLATE": ""}), \
                    mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "OPENAI_API_KEY", "clave"), \
                    mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                    mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso(banco.TODAS_LIB)), \
                    mock.patch.object(app, "post_openai_response", openai), \
                    mock.patch.object(app, "post_graph_message") as graph:
                self.assertEqual(app.app.test_client().post("/webhook", data=cuerpo, headers={
                    "Content-Type": "application/json", "X-Hub-Signature-256": firma}).status_code, 200)
            return [c[0][0] for c in graph.call_args_list]

        enviar(caso["cliente"], "wamid-oferta", caso["ia"])
        si = banco.ia()
        si["respuesta_cliente"] = banco.clasificacion("acepta")
        mensajes = enviar("Sí", "wamid-si", si)
        aviso = next(m["text"]["body"] for m in mensajes if m["to"] == "51900000001")
        self.assertIn("Inmueble: Libertador · unidad 502 · piso 5 · Pueblo Libre · S/ 289,460 (C2-268EAF11F560)", aviso)
        self.assertIn("Dirección: Jr. Los Pinos 450, Pueblo Libre", aviso)
        self.assertIn("Cuánto: Ofrece PEN 200,000 por Libertador · unidad 502", aviso)
        self.assertNotIn("400,000", aviso, "el presupuesto de búsqueda no aparece junto a la oferta")
        perfil = guardados[-1]["p_profile"]
        self.assertEqual(perfil["conversation"]["qualification"]["cuanto"],
                         {"price_offer": {"amount": 200000, "currency": "PEN", "reference": "C2-268EAF11F560"}})
        self.assertEqual(perfil["budget"], "PEN 400000", "el presupuesto de búsqueda sigue guardado en su campo")
        self.assertEqual(guardados[-1]["p_session"]["stage"], "handoff")


class UnidadEnElAvisoTest(unittest.TestCase):
    """El asesor reconoce el departamento sin buscar el código: proyecto, unidad, piso, distrito, precio y dirección."""

    def test_direccion_de_la_tarjeta(self):
        casos = {"📍**DIRECCIÓN**: Jr. Los Pinos 450, Pueblo Libre\n": "Jr. Los Pinos 450, Pueblo Libre",
                 "📍 **Dirección:** Dpto. C-407, Jr. Universo N°305, Chorrillos\n": "Dpto. C-407, Jr. Universo N°305, Chorrillos",
                 "📍DIRECCIÓN:  Av. Los Faisanes 485, Chorrillos**,\n": "Av. Los Faisanes 485, Chorrillos",
                 "📍 DIRECCION: Av. Talara 726, Jesús María, Lima\n": "Av. Talara 726, Jesús María, Lima",
                 "Sin dirección en esta tarjeta": "", "": ""}
        for tarjeta, esperado in casos.items():
            self.assertEqual(app.card_address(tarjeta), esperado, tarjeta)

    def test_datos_desde_el_inventario(self):
        with mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso()):
            unidad = app.advisor_unit("C2-268EAF11F560", [banco.LIBERTADOR])
        self.assertEqual(unidad["label"], "Libertador · unidad 502 · piso 5 · Pueblo Libre · S/ 289,460 (C2-268EAF11F560)")
        self.assertEqual(unidad["short"], "Libertador · unidad 502")
        self.assertEqual(unidad["address"], "Jr. Los Pinos 450, Pueblo Libre")

    def test_sin_datos_el_aviso_muestra_el_codigo(self):
        import requests
        import advisor_notify
        falla = mock.Mock()
        falla.unit_row.side_effect = requests.ConnectionError
        with mock.patch.object(app, "get_inventory_store", return_value=falla):
            self.assertIsNone(app.advisor_unit("C2-XYZ999", None))
        texto = advisor_notify.advisor_text("51900003549", {"request_type": "visit", "property_reference": "C2-XYZ999"},
                                            banco.CRITERIOS_PL, "Sí")
        self.assertIn("Inmueble: C2-XYZ999", texto)
        self.assertNotIn("Dirección:", texto)

    def test_plantilla_lleva_unidad_y_direccion(self):
        import advisor_notify
        unidad = {"reference": "C2-268EAF11F560", "short": "Libertador · unidad 502",
                  "label": "Libertador · unidad 502 · piso 5 · Pueblo Libre · S/ 289,460 (C2-268EAF11F560)",
                  "address": "Jr. Los Pinos 450, Pueblo Libre"}
        payload = advisor_notify.template_payload("51900000001", "aviso", "es", "51900003549",
                                                  {"request_type": "visit", "property_reference": "C2-268EAF11F560"},
                                                  banco.CRITERIOS_PL, unit=unidad)
        parametros = [p["text"] for p in payload["template"]["components"][0]["parameters"]]
        self.assertEqual(parametros[2], "Libertador · unidad 502 · piso 5 · Pueblo Libre · S/ 289,460 (C2-268EAF11F560) – Jr. Los Pinos 450, Pueblo Libre")


class TokensEnSupabaseTest(unittest.TestCase):
    """Cada mensaje guarda en extraction._uso_openai lo que gastó en OpenAI, para medir tokens sin los logs de Render."""

    def _openai(self, observacion, entrada, salida):
        respuesta = mock.Mock(status_code=200)
        respuesta.raise_for_status.return_value = None
        respuesta.json.return_value = {"model": "gpt-4.1-mini-2025-04-14", "output_text": json.dumps(observacion),
                                       "usage": {"input_tokens": entrada, "output_tokens": salida, "total_tokens": entrada + salida}}
        return respuesta

    def test_se_guarda_con_el_mensaje(self):
        import hashlib
        import hmac
        caso = next(c for c in banco.CASOS if c["id"] == "rep_ya_me_lo_dijiste")
        memoria = mock.Mock()
        memoria.claim_work.return_value = ("claimed", "token")
        memoria.load_session.return_value = caso["historia"]
        cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-tokens", "from": banco.TELEFONO, "type": "text", "text": {"body": caso["cliente"]}}]}}]}]}).encode()
        firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "OPENAI_API_KEY", "clave"), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso(banco.TODAS_LIB)), \
                mock.patch.object(app.requests, "post", return_value=self._openai(caso["ia"], 2431, 168)), \
                mock.patch.object(app, "post_graph_message"):
            self.assertEqual(app.app.test_client().post("/webhook", data=cuerpo, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": firma}).status_code, 200)
        guardado = memoria.commit_work.call_args[0][0]["p_inbound"]["extraction"]
        self.assertEqual(guardado["_uso_openai"], [{"tipo": "observacion", "modelo": "gpt-4.1-mini-2025-04-14",
                                                     "entrada": 2431, "salida": 168, "total": 2599, "ok": True}])

    def test_un_mensaje_no_arrastra_el_consumo_del_anterior(self):
        app.start_openai_usage()
        app.record_openai_usage({"instructions": "x"}, self._openai({}, 10, 5))
        self.assertEqual(len(app.openai_usage()), 1)
        app.start_openai_usage()
        self.assertEqual(app.openai_usage(), [])

    def test_nunca_rompe_el_turno(self):
        app.start_openai_usage()
        for respuesta in (mock.Mock(status_code=mock.Mock()), mock.Mock(status_code=500), object(), None):
            app.record_openai_usage({"instructions": "x"}, respuesta)
        rara = mock.Mock(status_code=200)
        rara.json.side_effect = ValueError
        app.record_openai_usage({"text": None}, rara)
        self.assertTrue(all(not llamada["ok"] or llamada["entrada"] is None for llamada in app.openai_usage()))


class FichaDelInventarioTest(unittest.TestCase):
    """La IA nunca escribe precio, área ni entrega en la ficha: esos datos salen del inventario."""

    def ficha(self, item, texto_ia, tarjeta="📍 DIRECCIÓN: Jr. Los Pinos 450"):
        with mock.patch.object(app, "project_card_text", return_value=tarjeta), \
                mock.patch.object(app, "ai_text", return_value=texto_ia) as ia, \
                mock.patch.object(app, "unit_notice_text", return_value=""):
            return app.full_unit_reply(item), ia

    def test_datos_del_inventario_aunque_la_ia_escriba_otros(self):
        texto, _ = self.ficha(banco.LIBERTADOR, "✨ Lindo depa.\n💰 PRECIO DE VENTA: S/ 999,999\n📐 Área: 200 m²\n"
                                               "📅 Entrega: Ene 2030\n🛏️ 5 dormitorios\nDisponible para mudarse ya")
        for dato in ("💰 PRECIO DE VENTA: S/ 289,460", "📐 Área: 56.4 m²", "🏢 Piso: 5", "🛏️ Dormitorios: 2",
                     "🛁 Baños: 2", "📅 Entrega: Dic 2027", "✨ Lindo depa."):
            self.assertIn(dato, texto)
        for inventado in ("999,999", "200 m²", "2030", "5 dormitorios", "Disponible"):
            self.assertNotIn(inventado, texto)

    def test_nunca_pen_ni_usd(self):
        dolares = copy.deepcopy(banco.UNIVERSO)
        dolares["public"]["currency"] = "USD"
        texto, _ = self.ficha(dolares, None)
        self.assertIn("💰 PRECIO DE VENTA: US$ 295,000", texto)
        texto, _ = self.ficha(banco.UNIVERSO, None)
        self.assertIn("💰 PRECIO DE VENTA: S/ 295,000", texto)
        self.assertNotRegex(texto, r"\b(PEN|USD)\b")

    def test_estacionamiento_siempre_igual_para_la_misma_unidad(self):
        """El 31/10 dos fichas de Universo dijeron 'no incluido' y 'por confirmar': ahora sale del registro."""
        casos = {None: "🚗 Estacionamiento: por confirmar", 0: "🚗 Sin estacionamiento incluido",
                 2: "🚗 2 estacionamientos, por confirmar si está incluido en el precio"}
        for espacios, linea in casos.items():
            item = copy.deepcopy(banco.UNIVERSO)
            item["public"]["parking_spaces"] = espacios
            self.assertEqual(app.ficha_parking_line(item), linea)
        item = copy.deepcopy(banco.UNIVERSO)
        item["public"].update(parking_spaces=1, public_description="Proyecto Universo; unidad 604. Un estacionamiento incluido según ficha.")
        self.assertEqual(app.ficha_parking_line(item), "🚗 1 estacionamiento incluido")
        primera, _ = self.ficha(banco.UNIVERSO, "🚗 Estacionamiento: no incluido")
        segunda, _ = self.ficha(banco.UNIVERSO, "🚗 Estacionamiento: incluido")
        self.assertEqual(primera.count("🚗"), 1)
        self.assertEqual(primera, segunda)

    def test_pregunta_de_cierre_segun_entrega(self):
        inmediata, _ = self.ficha(banco.UNIVERSO, None)
        futura, _ = self.ficha(banco.LIBERTADOR, "✨ Ok.\n¿Te gustaría agendar una visita hoy mismo?")
        self.assertIn("📅 Entrega inmediata", inmediata)
        self.assertIn("¿Quieres que coordinemos una visita al departamento?", inmediata)
        self.assertIn("¿Quieres que un asesor te envíe los planos y el brochure del proyecto?", futura)
        self.assertNotIn("agendar una visita hoy", futura, "la pregunta de la IA no reemplaza la del sistema")

    def test_sin_ia_sale_la_ficha_completa(self):
        texto, ia = self.ficha(banco.LIBERTADOR, None)
        self.assertIn("🏠 DEPARTAMENTO EN VENTA – LIBERTADOR, PUEBLO LIBRE", texto)
        self.assertIn("💰 PRECIO DE VENTA: S/ 289,460", texto)
        self.assertTrue(texto.endswith("Ref. C2-268EAF11F560"))
        _, ia = self.ficha(banco.LIBERTADOR, "✨ x", tarjeta="")
        ia.assert_not_called()

    def test_seccion_vacia_no_deja_titulo_suelto(self):
        texto, _ = self.ficha(banco.LIBERTADOR, "✨ Ok.\n📑 FINANCIAMIENTO\n💳 Separación con 2,000 soles\n🌟 ÁREAS COMUNES\n🛋️ SUM")
        self.assertNotIn("FINANCIAMIENTO", texto)
        self.assertIn("🌟 ÁREAS COMUNES\n🛋️ SUM", texto)

    def test_la_foto_toma_la_pregunta_de_cierre(self):
        """photo_turn sigue quitando la pregunta de cierre para ponerla bajo la última foto."""
        texto, _ = self.ficha(banco.LIBERTADOR, "✨ Ok.")
        self.assertNotIn("brochure", app.FICHA_CLOSING.sub("", texto))


class FichaFormatosRealesTest(unittest.TestCase):
    """Descripciones tal como vienen de Power BI, la lista de precios y Trello."""

    def facts(self, descripcion, precio=443000, **extra):
        public = {**banco.LIBERTADOR["public"], "public_description": descripcion, "price_amount": precio, **extra}
        return app.ficha_unit_facts({"public": public, "delivery": app.property_delivery(public)})

    def test_entrega_por_confirmar_de_la_lista(self):
        _, _, facts = self.facts("Proyecto Mondrian; unidad 806-B. Vista INTERIOR B. Entrega según fuente: X Confirmar. Estacionamiento no confirmado como incluido en el precio.")
        self.assertIn("📅 Entrega: por confirmar", facts)
        self.assertIn("🌄 Vista: Interior B", facts)

    def test_fecha_corta_de_power_bi(self):
        _, _, facts = self.facts("Proyecto TORRE TALE; unidad A-2103. Piso 21. Vista Exterior. Entrega según fuente: May-27. Estacionamiento no confirmado como incluido en el precio.")
        self.assertIn("📅 Entrega: May 2027", facts)
        self.assertIn("🏢 Piso: 21", facts)

    def test_precio_con_centimos_no_se_redondea(self):
        _, precio, _ = self.facts("Proyecto Unico Canevaro; unidad 702. Piso 7. Entrega según fuente: Abril 2028.", precio=465070.87)
        self.assertEqual(precio[0], "💰 PRECIO DE VENTA: S/ 465,070.87")


class MismoPrecioEnTodosLadosTest(unittest.TestCase):
    """Una unidad con céntimos muestra el mismo precio en el listado, la ficha, '¿cuánto cuesta?', la contraoferta
    y el aviso al asesor (antes la ficha decía S/ 465,070.87 y el listado S/ 465,071)."""

    def test_formato(self):
        self.assertEqual(app.format_price(289460, "PEN"), "S/ 289,460")
        self.assertEqual(app.format_price("465070.87", "PEN"), "S/ 465,070.87")
        self.assertEqual(app.format_price(465070.80, "PEN"), "S/ 465,070.80")
        self.assertEqual(app.format_price(190288.0, "USD"), "US$ 190,288")
        self.assertEqual(app.format_price(337995.999, "PEN"), "S/ 337,996")

    def test_todos_los_mensajes_coinciden(self):
        item = copy.deepcopy(banco.LIBERTADOR)
        item["public"]["price_amount"] = 465070.87
        esperado = "S/ 465,070.87"
        with mock.patch.object(app, "project_card_text", return_value=""), \
                mock.patch.object(app, "unit_notice_text", return_value=""), \
                mock.patch.object(app, "get_inventory_store", return_value=None):
            mensajes = {"listado": app.brief_listing([item]), "listado antiguo": app.inventory_reply([item]),
                        "ficha": app.full_unit_reply(item), "cuánto cuesta": app.unit_fact_reply(item, "precio"),
                        "contraoferta": app.price_proposal_reply(item),
                        "aviso al asesor": app.advisor_unit("C2-268EAF11F560", [item])["label"]}
        for donde, texto in mensajes.items():
            self.assertIn(esperado, texto, donde)
            self.assertNotIn("465,071", texto, donde)


class TextoOriginalDelClienteTest(unittest.TestCase):
    """El bot decide con el texto normalizado, pero el historial, el lead y el aviso al asesor guardan lo que el
    cliente escribió: el 07/11, "ok gracias" quedó como "Sí" y el asesor habría leído "Último mensaje: Sí"."""

    def test_historial_lead_y_aviso(self):
        import hashlib
        import hmac
        caso = next(c for c in banco.CASOS if c["id"] == "negociacion_si_al_asesor")
        memoria = mock.Mock()
        memoria.claim_work.return_value = ("claimed", "token")
        memoria.load_session.return_value = caso["historia"]
        cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-original", "from": banco.TELEFONO, "type": "text", "text": {"body": "ok gracias"}}]}}]}]}).encode()
        firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on", "ADVISOR_PHONE": "51900000001", "ADVISOR_TEMPLATE": ""}), \
                mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "OPENAI_API_KEY", "clave"), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso(banco.TODAS_LIB)), \
                mock.patch.object(app, "post_openai_response", banco.OpenAIFalso(caso["ia"])) as openai, \
                mock.patch.object(app, "post_graph_message") as graph:
            self.assertEqual(app.app.test_client().post("/webhook", data=cuerpo, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": firma}).status_code, 200)
        guardado = memoria.commit_work.call_args[0][0]
        self.assertEqual(app.normalize_client_text("ok gracias"), "Sí", "el bot decide con el texto normalizado")
        self.assertEqual(guardado["p_session"]["stage"], "handoff")
        self.assertEqual(guardado["p_inbound"]["content"], "ok gracias")
        self.assertEqual(guardado["p_profile"]["conversation"]["last_user_message"], "ok gracias")
        aviso = next(c[0][0]["text"]["body"] for c in graph.call_args_list if c[0][0]["to"] == "51900000001")
        self.assertIn("Último mensaje: “ok gracias”", aviso)


class ListoDelAsesorTest(unittest.TestCase):
    """El "✅ Listo" a un asesor guarda si Meta lo aceptó o el código con que lo rechazó (11/11: dos saludos sin
    respuesta y la causa solo en los logs de Render)."""

    def enviar_hola(self, graph):
        import hashlib
        import hmac
        memoria = mock.Mock()
        memoria.claim_work.return_value = ("claimed", "token")
        cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-hola", "from": "51947475550", "type": "text", "text": {"body": "Hola"}}]}}]}]}).encode()
        firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, {"ADVISOR_PHONE": "51947475550,51996353549"}), \
                mock.patch.object(app, "APP_SECRET", "secreto"), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                mock.patch.object(app, "post_graph_message", graph):
            self.assertEqual(app.app.test_client().post("/webhook", data=cuerpo, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": firma}).status_code, 200)
        return memoria

    def test_enviado(self):
        graph = mock.Mock()
        memoria = self.enviar_hola(graph)
        self.assertIn("✅ Listo", graph.call_args[0][0]["text"]["body"])
        memoria.finish_work.assert_called_once_with("wamid-hola", "token")
        memoria.record_delivery.assert_called_once_with("wamid-hola", "sent", None)

    def test_rechazado_guarda_el_codigo_de_meta(self):
        import requests
        respuesta = mock.Mock(status_code=400)
        respuesta.json.return_value = {"error": {"code": 131047, "message": "Re-engagement message"}}
        graph = mock.Mock(side_effect=requests.HTTPError("rechazado", response=respuesta))
        memoria = self.enviar_hola(graph)
        memoria.finish_work.assert_called_once_with("wamid-hola", "token")
        memoria.record_delivery.assert_called_once_with("wamid-hola", "failed", "meta_http_400_131047")

    def test_codigos(self):
        import requests
        sin_respuesta = requests.ConnectionError("sin red")
        self.assertEqual(app.meta_failure_code(sin_respuesta), "meta_transport")
        rara = mock.Mock(status_code=500)
        rara.json.side_effect = ValueError
        self.assertEqual(app.meta_failure_code(requests.HTTPError(response=rara)), "meta_http_500")


class FinishWorkTest(unittest.TestCase):
    """finish_processed_message devuelve un booleano suelto: leerlo como filas daba TypeError y HTTP 500 en cada
    saludo de asesor y en cada mensaje con el bot en pausa (log de Render del 11/11, 15:29)."""

    def store(self, devuelve):
        respuesta = mock.Mock(status_code=200)
        respuesta.raise_for_status.return_value = None
        respuesta.json.return_value = devuelve
        tienda = app.ConversationMemoryStore.__new__(app.ConversationMemoryStore)
        tienda.url, tienda.headers = "https://db.test", {}
        return tienda, respuesta

    def test_formas_de_respuesta(self):
        for devuelve, esperado in ((True, True), (False, False), ([{"finish_processed_message": True}], True),
                                   ([{"finish_processed_message": False}], False), ([], False), (None, False)):
            tienda, respuesta = self.store(devuelve)
            with mock.patch.object(app.requests, "post", return_value=respuesta):
                self.assertEqual(tienda.finish_work("wamid-x", "token"), esperado, repr(devuelve))


class NuevaBusquedaNaturalTest(unittest.TestCase):
    def test_frases_que_reinician(self):
        for frase in ("Quiero otro nuevo", "Haz una nueva evaluación, te brindaré otros datos", "quiero empezar de cero",
                      "empecemos desde cero", "hagamos una nueva búsqueda", "quiero cambiar mi búsqueda", "otra búsqueda porfa",
                      "comenzar de nuevo", "reiniciar"):
            self.assertTrue(app.wants_new_search(frase), frase)
            self.assertEqual(app.resume_answer_as_command(None, frase), "nueva búsqueda", frase)

    def test_frases_que_no(self):
        for frase in ("no, pero muéstrame otra", "quiero ver otro departamento", "tengo una nueva consulta: ¿tiene cochera?",
                      "busco otro depa en Surco", "otra vez lo mismo?", "mejor en Surco", "¿hay otras opciones?"):
            self.assertFalse(app.wants_new_search(frase), frase)
