"""Estilo ARENZ: emoticons en los mensajes fijos, guía de estilo para la IA y agentes inmobiliarios."""
import unittest
from unittest import mock

import app
import banco_conversaciones as banco


def turno_reglas(texto, previous=None):
    """plan_turn con el inventario simulado y sin llamadas a OpenAI (las reglas responden)."""
    with mock.patch.object(app, "OPENAI_API_KEY", ""), \
            mock.patch.object(app, "conversacion_ia_activa", return_value=False):
        return app.plan_turn(banco.TELEFONO, previous, texto, None, "FALLBACK", banco.InventarioFalso())


class EmoticonsTest(unittest.TestCase):
    def test_mensajes_fijos_llevan_emoticon_al_final_o_tras_la_presentacion(self):
        self.assertIn("😊", app.INTRODUCTION)
        self.assertTrue(app.INTRODUCTION.startswith(f"Hola, soy {app.ASSISTANT_NAME}"))
        self.assertTrue(app.OFFER_DECLINED_REPLY.startswith("Entendido, no te contactará un asesor"))
        self.assertTrue(app.CLOSING_REPLY.startswith("Gracias por escribir a"))
        self.assertIn("🙌", app.handoff_reply({"request_type": "visit", "property_reference": "C2-A"})[0])

    def test_despedida_guardada_sin_emoticon_se_sigue_reconociendo(self):
        antigua = {"recent_messages": [{"direction": "assistant", "content": app.CLOSING_THANKS_BASE}]}
        with mock.patch.object(app, "recent_conversation_turns",
                               side_effect=lambda previous, limit=8, raw=False: previous["recent_messages"]):
            self.assertEqual(app.bot_offer(antigua), "cerrada")

    def test_emoticon_suelto_no_descarta_la_respuesta_de_la_ia(self):
        self.assertTrue(app.assistant_reply_is_real_estate_scoped("¡Perfecto! 😊 ¿En qué distrito buscas tu departamento? 🏡"))
        self.assertFalse(app.assistant_reply_is_real_estate_scoped("¡Qué buen clima! 😊"))

    def test_la_ia_recibe_la_guia_de_estilo(self):
        for frase in ("emoticons", "no supongas que le interesa", "No prometas descuentos", "agente o corredor",
                      "No preguntes si necesita cochera"):
            self.assertIn(frase, app.COMMERCIAL_POLICY)


class PrimerContactoTest(unittest.TestCase):
    def test_primer_mensaje_se_presenta_sin_nueva_busqueda(self):
        texto = app.first_contact_reply("Nueva búsqueda iniciada. " + app.DISTRICT_QUESTION)
        self.assertEqual(texto, f"¡Hola! 😊 Soy {app.ASSISTANT_NAME}, de {app.COMMERCIAL_BRAND}. {app.DISTRICT_QUESTION}")
        self.assertTrue(app.first_contact_reply(app.INTRODUCTION, "Ana").startswith("¡Hola, Ana! 😊 Soy"))

    def test_solo_en_el_primer_contacto(self):
        self.assertTrue(app.is_first_contact(None))
        previous = banco.historial("qualification", {}, "hola", app.INTRODUCTION)
        self.assertFalse(app.is_first_contact(previous))
        texto = turno_reglas("busco en Surco", previous)["plan"]["text"]
        self.assertFalse(texto.startswith("¡Hola!"))


class NombreDelClienteTest(unittest.TestCase):
    def test_extrae_el_nombre(self):
        casos = {("me llamo Ana Pérez", False): "Ana Pérez", ("Hola, soy Joanna, agente inmobiliaria", False): "Joanna",
                 ("mi nombre es luis", False): "Luis", ("Juan", True): "Juan", ("Hola, María", True): "María",
                 ("Juan Carlos Pérez", True): "Juan Carlos"}
        for (texto, pedido), esperado in casos.items():
            self.assertEqual(app.name_from_text(texto, asked=pedido), esperado, texto)
        for texto, pedido in (("soy de Lima", True), ("soy agente inmobiliaria", True), ("busco en Surco", True),
                              ("no", True), ("Juan", False), ("tengo 300 mil", True), ("hola", True)):
            self.assertIsNone(app.name_from_text(texto, asked=pedido), texto)

    def test_primer_saludo_pide_el_nombre_y_guarda_la_pregunta(self):
        turno = turno_reglas("Hola")
        self.assertIn(app.NAME_QUESTION, turno["plan"]["text"])
        self.assertTrue(turno["plan"]["text"].startswith(f"¡Hola! 😊 Soy {app.ASSISTANT_NAME}"))
        self.assertEqual(turno["search_state"]["pending_question"], app.DISTRICT_QUESTION)

    def test_respuesta_con_el_nombre_retoma_la_pregunta_pendiente(self):
        previous = banco.historial("qualification", {}, "Hola", "¡Hola! 😊 Soy Cecilia. " + app.NAME_QUESTION)
        previous["state"]["pending_question"] = app.DISTRICT_QUESTION
        turno = turno_reglas("Juan", previous)
        self.assertEqual(turno["plan"]["text"], f"Mucho gusto, Juan 😊 {app.DISTRICT_QUESTION}")
        self.assertEqual(turno["search_state"]["customer_name"], "Juan")

    def test_si_no_da_el_nombre_sigue_la_busqueda_sin_insistir(self):
        previous = banco.historial("qualification", {}, "Hola", "¡Hola! 😊 Soy Cecilia. " + app.NAME_QUESTION)
        texto = turno_reglas("busco en Surco", previous)["plan"]["text"]
        self.assertNotIn("Mucho gusto", texto)
        self.assertNotIn(app.NAME_QUESTION, texto)

    def test_primer_mensaje_con_nombre_saluda_por_su_nombre(self):
        turno = turno_reglas("Hola, me llamo Ana")
        self.assertTrue(turno["plan"]["text"].startswith("¡Hola, Ana! 😊"))
        self.assertNotIn(app.NAME_QUESTION, turno["plan"]["text"])
        self.assertEqual(turno["search_state"]["customer_name"], "Ana")

    def test_el_nombre_se_guarda_en_la_sesion(self):
        memoria = mock.Mock()
        previous = {"stage": "qualification", "state": {"customer_name": "Ana", "pending_question": app.DISTRICT_QUESTION}}
        app.commit_consistent_turn(memoria, "token", banco.TELEFONO, "m1", "busco en Surco", "ok", {}, {}, "qualification",
                                   "resumen", previous, {"active_search_id": None, "searches": {}})
        sesion = memoria.commit_work.call_args[0][0]["p_session"]["state"]
        self.assertEqual(sesion["customer_name"], "Ana")
        self.assertNotIn("pending_question", sesion)


class ConversacionPersistidaTest(unittest.TestCase):
    """Turnos seguidos como en producción: cada uno parte de lo que commit_consistent_turn guardó en la sesión."""

    def conversar(self, *mensajes):
        previous, respuestas = None, []
        for numero, mensaje in enumerate(mensajes):
            turno = turno_reglas(mensaje, previous)
            memoria = mock.Mock()
            app.commit_consistent_turn(memoria, "token", banco.TELEFONO, f"m{numero}", mensaje, turno["plan"]["stored"], {},
                                       turno["criteria"], turno["stage"], turno["summary"], previous, turno["search_state"])
            pago = memoria.commit_work.call_args[0][0]
            estado = dict(pago["p_session"]["state"])
            if estado.get("active_search_id"):  # load_session une la búsqueda activa a la sesión
                estado["searches"] = {estado["active_search_id"]: pago["p_search"]["state"]}
            previous = {"stage": pago["p_session"]["stage"], "state": estado, "summary": pago["p_session"]["summary"]}
            respuestas.append(turno["plan"]["text"])
        return respuestas, previous

    def test_saludo_nombre_y_distrito_sin_volver_a_presentarse(self):
        respuestas, previous = self.conversar("Hola", "Rildo", "Surco")
        self.assertIn(app.NAME_QUESTION, respuestas[0])
        self.assertEqual(respuestas[1], f"Mucho gusto, Rildo 😊 {app.DISTRICT_QUESTION}")
        self.assertFalse(respuestas[2].startswith(("¡Hola", "Hola")), respuestas[2])
        self.assertEqual(app.customer_name(previous), "Rildo")

    def test_sin_busqueda_el_historial_queda_en_la_sesion(self):
        _, previous = self.conversar("Hola")
        self.assertEqual([t["direction"] for t in previous["state"]["recent_turns"]], ["user", "assistant"])
        self.assertTrue(app.asked_for_name(previous))


class NoEntendioTest(unittest.TestCase):
    PREGUNTA_PLAZO = ("Perfecto, gracias. Último dato antes de mostrarte las opciones: ¿para cuándo necesitas el departamento? "
                      "Por ejemplo: lo antes posible, en los próximos meses o sin apuro.")

    def test_si_repite_la_pregunta_dice_que_no_entendio(self):
        previous = banco.historial("qualification", {}, "Crédito", self.PREGUNTA_PLAZO)
        repetida = ("Tengo opciones que podrían encajar con lo que buscas. Antes de mostrártelas, ¿para cuándo necesitas el "
                    "departamento? Por ejemplo: lo antes posible, en los próximos meses o sin apuro.")
        self.assertEqual(app.repeated_question_reply(previous, repetida),
                         "Perdona, no te entendí bien 😅 ¿Para cuándo necesitas el departamento? Por ejemplo: lo antes "
                         "posible, en los próximos meses o sin apuro.")

    def test_otra_pregunta_o_un_dato_no_se_tocan(self):
        previous = banco.historial("qualification", {}, "Crédito", self.PREGUNTA_PLAZO)
        self.assertIsNone(app.repeated_question_reply(previous, "¿Cuántos dormitorios necesitas?"))
        ficha = banco.historial("property_detail", banco.CRITERIOS_PL, "1", banco.DESCRIPCION_1850)
        self.assertIsNone(app.repeated_question_reply(ficha, "Precio: S/ 289,460. ¿Quieres saber sobre la ubicación o áreas comunes?"))

    def test_ahorq_pasa_a_la_siguiente_pregunta(self):
        self.assertEqual(app.normalize_client_text("Ahorq"), "Lo antes posible")


class AgenteInmobiliarioTest(unittest.TestCase):
    def test_reconoce_agentes_que_buscan_para_un_cliente(self):
        for texto in ("Hola, soy agente inmobiliaria y busco propiedad para un cliente",
                      "somos corredores, ¿comparten comisión?", "mi cliente busca un depa en Surco",
                      "Soy asesora inmobiliaria"):
            self.assertTrue(app.is_broker_message(texto), texto)
        for texto in ("Hola", "busco un departamento en Surco", "soy de Lima", "quiero hablar con un asesor"):
            self.assertFalse(app.is_broker_message(texto), texto)

    def test_responde_colaboracion_y_comision(self):
        turno = turno_reglas("Hola, soy Joanna, agente inmobiliaria. Busco propiedad para un cliente")
        texto = turno["plan"]["text"]
        self.assertIn("colaboración con agentes inmobiliarios", texto)
        self.assertIn("50 % para cada parte", texto)
        self.assertIn("busca tu cliente", texto)
        self.assertTrue(turno["summary"].startswith(app.BROKER_SUMMARY))

    def test_conserva_el_listado_si_el_agente_ya_dio_los_criterios(self):
        listado = banco.LISTADO_PL
        texto = app.broker_reply(listado, "FALLBACK")
        self.assertIn("colaboración con agentes", texto)
        self.assertTrue(texto.endswith(listado))

    def test_sin_comision_configurada_no_la_menciona(self):
        with mock.patch.object(app, "AGENT_COMMISSION_SHARE", ""):
            texto = turno_reglas("soy corredor, busco para un cliente")["plan"]["text"]
        self.assertIn("colaboración con agentes inmobiliarios.", texto)
        self.assertNotIn("comisión", texto)

    def test_no_repite_las_condiciones_si_ya_se_dijeron(self):
        ya_dicho = app.broker_reply(None)
        previous = banco.historial("qualification", {}, "soy agente, busco para un cliente", ya_dicho)
        texto = turno_reglas("mi cliente busca en Surco", previous)["plan"]["text"]
        self.assertNotIn("colaboración con agentes", texto)


if __name__ == "__main__":
    unittest.main()
