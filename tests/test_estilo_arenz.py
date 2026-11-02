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
        for frase in ("emoticons", "no supongas que le interesa", "No prometas descuentos", "agente o corredor"):
            self.assertIn(frase, app.COMMERCIAL_POLICY)


class PrimerContactoTest(unittest.TestCase):
    def test_primer_mensaje_se_presenta_sin_nueva_busqueda(self):
        texto = app.first_contact_reply("Nueva búsqueda iniciada. " + app.DISTRICT_QUESTION)
        self.assertEqual(texto, f"¡Hola! 😊 Soy {app.ASSISTANT_NAME}, de {app.COMMERCIAL_BRAND}. {app.DISTRICT_QUESTION}")
        self.assertEqual(app.first_contact_reply(app.INTRODUCTION), app.INTRODUCTION)

    def test_solo_en_el_primer_contacto(self):
        self.assertTrue(app.is_first_contact(None))
        previous = banco.historial("qualification", {}, "hola", app.INTRODUCTION)
        self.assertFalse(app.is_first_contact(previous))
        texto = turno_reglas("busco en Surco", previous)["plan"]["text"]
        self.assertFalse(texto.startswith("¡Hola!"))


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
