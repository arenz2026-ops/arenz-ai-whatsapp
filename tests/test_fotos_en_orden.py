"""La pregunta que va debajo de una tanda de fotos se envía cuando WhatsApp confirma que llegaron todas.

13/11, Mar de Grau: el bot pegó "📷 Tengo 4 fotos más… ¿Quieres que te envíe más fotos?" a la última de 5 fotos, pero
WhatsApp entregó esa foto primero y la pregunta quedó arriba. WhatsApp no garantiza el orden de entrega.
"""
import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")

import requests  # noqa: E402

import app  # noqa: E402
import banco_conversaciones as banco  # noqa: E402

PREGUNTA = "📷 Tengo 4 fotos más de este proyecto. ¿Quieres que te envíe más fotos?"


class TandasFalsas:
    """Lo mismo que photo_batch_create / photo_batch_delivered en Supabase, en memoria."""

    def __init__(self):
        self.tandas, self.vistos = [], set()

    def photo_batch_create(self, phone, body, message_ids):
        pendientes = [m for m in message_ids if m not in self.vistos]
        self.tandas.append({"phone": phone, "body": body, "pendientes": pendientes, "enviada": not pendientes})
        return not pendientes

    def photo_batch_delivered(self, phone, message_id):
        for tanda in self.tandas:
            if not tanda["enviada"] and message_id in tanda["pendientes"]:
                tanda["pendientes"].remove(message_id)
                if not tanda["pendientes"]:
                    tanda["enviada"] = True
                    return [{"phone": tanda["phone"], "body": tanda["body"]}]
                return []
        self.vistos.add(message_id)
        return []


def graph_falso():
    """post_graph_message que devuelve un wamid distinto por mensaje y recuerda lo enviado."""
    enviados = []

    def enviar(payload):
        enviados.append(payload)
        respuesta = mock.Mock(status_code=200)
        respuesta.json.return_value = {"messages": [{"id": f"wamid.{len(enviados)}"}]}
        return respuesta
    return enviar, enviados


def fotos(n, pregunta=PREGUNTA):
    lista = [{"reference": "C2-1E4D46D54B61", "url": f"https://x/{i}.jpg"} for i in range(1, n + 1)]
    lista[-1]["caption"] = pregunta
    return lista


class EnvioTest(unittest.TestCase):
    def enviar(self, plan, memoria):
        enviar, enviados = graph_falso()
        with mock.patch.object(app, "post_graph_message", enviar):
            app.send_photos_with_follow_up(memoria, "51900003549", plan, None)
        return enviados

    def test_tanda_sin_pregunta_hasta_que_lleguen_todas(self):
        memoria = TandasFalsas()
        enviados = self.enviar({"text": None, "photos": fotos(5)}, memoria)
        self.assertEqual([p["type"] for p in enviados], ["image"] * 5)
        self.assertFalse(any("caption" in p["image"] for p in enviados), "ninguna foto lleva la pregunta")
        self.assertEqual(memoria.tandas[0]["pendientes"], [f"wamid.{i}" for i in range(1, 6)])

    def test_si_ya_llegaron_todas_al_registrar_la_envia_ya(self):
        memoria = TandasFalsas()
        memoria.vistos = {f"wamid.{i}" for i in range(1, 6)}
        enviados = self.enviar({"text": None, "photos": fotos(5)}, memoria)
        self.assertEqual(enviados[-1]["text"]["body"], PREGUNTA)

    def test_si_supabase_falla_la_envia_ya(self):
        memoria = mock.Mock()
        memoria.photo_batch_create.side_effect = requests.ConnectionError
        enviados = self.enviar({"text": None, "photos": fotos(5)}, memoria)
        self.assertEqual(enviados[-1]["text"]["body"], PREGUNTA)

    def test_una_sola_foto_conserva_su_pie(self):
        memoria = TandasFalsas()
        enviados = self.enviar({"text": "ficha", "photos": fotos(1)}, memoria)
        self.assertEqual(enviados[0]["image"]["caption"], PREGUNTA)
        self.assertEqual(memoria.tandas, [])

    def test_sin_memoria_queda_como_antes(self):
        enviados = self.enviar({"text": None, "photos": fotos(3)}, None)
        self.assertEqual(enviados[-1]["image"]["caption"], PREGUNTA)


class AvisosDeEntregaTest(unittest.TestCase):
    """De punta a punta por /webhook: 'Sí' a más fotos, luego los avisos de entrega de WhatsApp."""

    def post(self, cuerpo):
        datos = json.dumps(cuerpo).encode()
        firma = "sha256=" + hmac.new(b"secreto", datos, hashlib.sha256).hexdigest()
        return app.app.test_client().post("/webhook", data=datos, headers={"Content-Type": "application/json",
                                                                         "X-Hub-Signature-256": firma})

    def aviso(self, wamid, estado="delivered"):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"statuses": [
            {"id": wamid, "status": estado, "recipient_id": banco.TELEFONO}]}}]}]}

    def test_la_pregunta_sale_despues_de_la_ultima_foto(self):
        caso = next(c for c in banco.CASOS if c["id"] == "real_1840_si_a_mas_fotos")
        tandas = TandasFalsas()
        memoria = mock.Mock(photo_batch_create=tandas.photo_batch_create, photo_batch_delivered=tandas.photo_batch_delivered)
        memoria.claim_work.return_value = ("claimed", "token")
        memoria.load_session.return_value = caso["historia"]
        enviar, enviados = graph_falso()
        mensaje = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-si", "from": banco.TELEFONO, "type": "text", "text": {"body": "Sí"}}]}}]}]}
        with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}), \
                mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "OPENAI_API_KEY", "clave"), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=memoria), \
                mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso(caso["enviadas"])), \
                mock.patch.object(app, "post_openai_response", banco.OpenAIFalso(caso["ia"])), \
                mock.patch.object(app, "crm_bot_paused", return_value=False), \
                mock.patch.object(app, "post_graph_message", enviar):
            self.assertEqual(self.post(mensaje).status_code, 200)
            imagenes = [p for p in enviados if p["type"] == "image"]
            self.assertEqual(len(imagenes), 5)
            self.assertFalse(any(p["type"] == "text" for p in enviados), "la pregunta todavía no sale")
            ids = [f"wamid.{i}" for i in range(1, len(enviados) + 1) if enviados[i - 1]["type"] == "image"]
            # Avisos en desorden, uno repetido, y otros ignorados ('read', 'sent').
            for wamid, estado in [(ids[4], "delivered"), (ids[0], "read"), (ids[1], "delivered"), (ids[1], "delivered"),
                                  (ids[0], "delivered"), (ids[3], "sent"), (ids[3], "delivered")]:
                self.assertEqual(self.post(self.aviso(wamid, estado)).status_code, 200)
                self.assertFalse(any(p["type"] == "text" for p in enviados), "aún falta una foto")
            respuesta = self.post(self.aviso(ids[2], "failed"))  # la última pendiente (una foto fallida también cuenta)
            self.assertEqual(respuesta.get_json(), {"status": "statuses", "follow_ups": 1})
            textos = [p["text"]["body"] for p in enviados if p["type"] == "text"]
            self.assertEqual(len(textos), 1)
            self.assertIn("¿Quieres que te envíe más fotos?", textos[0])
            self.post(self.aviso(ids[2], "delivered"))
            self.assertEqual(len([p for p in enviados if p["type"] == "text"]), 1, "nunca se envía dos veces")

    def test_aviso_sin_memoria_no_rompe(self):
        with mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "get_conversation_memory_store", return_value=None):
            respuesta = self.post(self.aviso("wamid.x"))
        self.assertEqual(respuesta.status_code, 200)

    def test_supabase_caido_no_rompe_el_webhook(self):
        memoria = mock.Mock()
        memoria.photo_batch_delivered.side_effect = requests.ConnectionError
        with mock.patch.object(app, "APP_SECRET", "secreto"), mock.patch.object(app, "get_conversation_memory_store", return_value=memoria):
            self.assertEqual(self.post(self.aviso("wamid.x")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
