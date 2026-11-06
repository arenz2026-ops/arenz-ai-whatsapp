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
    def test_apagado_por_defecto(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONVERSACION_IA", None)
            self.assertFalse(app.conversacion_ia_activa())

    def test_valores_reconocidos(self):
        for valor, esperado in (("on", True), ("ON", True), (" 1 ", True), ("true", True), ("sí", True),
                                ("off", False), ("0", False), ("", False), ("no", False), ("cualquiera", False)):
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": valor}):
                self.assertEqual(app.conversacion_ia_activa(), esperado, valor)

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
