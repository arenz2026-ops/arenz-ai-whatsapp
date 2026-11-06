"""Integración con el CRM CREENZIA: origen del anuncio de Meta y decisión de seguimiento."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import app
import followups

AHORA = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)

MENSAJE_DESDE_ANUNCIO = {
    "from": "51911111111", "id": "wamid.1", "type": "text", "text": {"body": "Hola, quiero más información"},
    "referral": {"source_url": "https://fb.me/abc", "source_id": "120211234567890", "source_type": "ad",
                 "headline": "Depa en Lince desde S/ 450 mil", "body": "Entrega inmediata", "ctwa_clid": "ARAkL1"},
}


def respuesta(json=None, status=200):
    r = mock.Mock()
    r.json.return_value = json if json is not None else []
    r.status_code = status
    r.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(response=r)
    return r


class OrigenAnuncioTest(unittest.TestCase):
    def test_lee_la_etiqueta_del_anuncio(self):
        fila = app.ad_referral_row("51911111111", MENSAJE_DESDE_ANUNCIO)
        self.assertEqual(fila["phone"], "51911111111")
        self.assertEqual(fila["source_id"], "120211234567890")
        self.assertEqual(fila["source_type"], "ad")
        self.assertEqual(fila["headline"], "Depa en Lince desde S/ 450 mil")
        self.assertEqual(fila["ctwa_clid"], "ARAkL1")

    def test_sin_anuncio_no_hay_etiqueta(self):
        sin = {k: v for k, v in MENSAJE_DESDE_ANUNCIO.items() if k != "referral"}
        self.assertIsNone(app.ad_referral_row("51911111111", sin))
        self.assertIsNone(app.ad_referral_row("51911111111", {**sin, "referral": {"source_type": "ad"}}))

    def test_guarda_la_etiqueta_en_el_crm(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={"apikey": "k"})
        with mock.patch.object(app.requests, "post", return_value=respuesta()) as post:
            self.assertTrue(app.record_ad_referral(memory, "51911111111", MENSAJE_DESDE_ANUNCIO))
        self.assertIn("/rest/v1/crm_ad_referrals", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["source_id"], "120211234567890")

    def test_si_falla_el_guardado_no_rompe_la_atencion(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        with mock.patch.object(app.requests, "post", side_effect=requests.ConnectionError()):
            self.assertFalse(app.record_ad_referral(memory, "51911111111", MENSAJE_DESDE_ANUNCIO))

    def test_mensaje_normal_no_escribe_nada(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        sin = {k: v for k, v in MENSAJE_DESDE_ANUNCIO.items() if k != "referral"}
        with mock.patch.object(app.requests, "post") as post:
            self.assertFalse(app.record_ad_referral(memory, "51911111111", sin))
        post.assert_not_called()


def sesion(telefono):
    return {"phone": telefono, "stage": "qualified", "state": {}, "summary": "",
            "updated_at": (AHORA - timedelta(hours=21)).isoformat()}


class DecisionSeguimientoTest(unittest.TestCase):
    def tienda(self, crm):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        store = followups.FollowupStore(memory)
        sesiones = [sesion("51900000001"), sesion("51900000002")]

        def get(url, **kwargs):
            if "conversation_sessions" in url:
                return respuesta(sesiones)
            return crm(url, **kwargs)
        return store, get

    def test_respeta_el_no_marcado_en_el_crm(self):
        store, get = self.tienda(lambda url, **kw: respuesta([{"phone": "51900000002"}]))
        with mock.patch.object(followups.requests, "get", side_effect=get):
            telefonos = [s["phone"] for s in store.due_sessions(AHORA, followups.settings())]
        self.assertEqual(telefonos, ["51900000001"])

    def test_sin_decision_se_mantiene_el_seguimiento_gratis(self):
        store, get = self.tienda(lambda url, **kw: respuesta([]))
        with mock.patch.object(followups.requests, "get", side_effect=get):
            self.assertEqual(len(store.due_sessions(AHORA, followups.settings())), 2)

    def test_si_no_se_puede_leer_el_crm_no_escribe_a_nadie(self):
        def caido(url, **kw):
            raise requests.ConnectionError()
        store, get = self.tienda(caido)
        with mock.patch.object(followups.requests, "get", side_effect=get):
            self.assertEqual(store.due_sessions(AHORA, followups.settings()), [])


if __name__ == "__main__":
    unittest.main()


LEAD_ID = "5ae22305-ceb6-4c56-a206-97caae142389"


class FakeHTTP:
    """Simula Supabase y la Graph API de Meta; guarda lo que el bot escribe."""

    def __init__(self, user_ok=True, paused=False, graph_error=None):
        self.user_ok, self.paused, self.graph_error = user_ok, paused, graph_error
        self.posts, self.patches = [], []

    def get(self, url, **kwargs):
        if url.endswith("/auth/v1/user"):
            return respuesta({"id": "auth-1"}, 200 if self.user_ok else 401)
        if url.endswith("/rest/v1/crm_users"):
            return respuesta([{"id": "u-rildo", "full_name": "Rildo Roque"}])
        if url.endswith("/rest/v1/crm_leads"):
            params = kwargs.get("params", {})
            if params.get("select") == "bot_paused":
                return respuesta([{"bot_paused": self.paused}])
            return respuesta([{"id": LEAD_ID, "phone": "51911111111"}])
        raise AssertionError(url)

    def post(self, url, **kwargs):
        if "graph.facebook.com" in url:
            if self.graph_error:
                r = respuesta({"error": {"code": self.graph_error}}, 400)
                return r
            self.posts.append(("graph", kwargs["json"]))
            return respuesta({"messages": [{"id": "wamid.x"}]})
        self.posts.append((url.split("/rest/v1/")[-1].split("?")[0], kwargs.get("json")))
        return respuesta()

    def patch(self, url, **kwargs):
        self.patches.append((url.split("/rest/v1/")[-1], kwargs.get("params"), kwargs.get("json")))
        return respuesta()


class ResponderDesdeCrmTest(unittest.TestCase):
    def enviar(self, fake, body=None, token="tok", origin="https://creenzia-crm.vercel.app"):
        memory = app.ConversationMemoryStore("https://x.supabase.co", "service-key")
        with mock.patch.object(app, "get_conversation_memory_store", return_value=memory), \
             mock.patch.object(app.requests, "get", side_effect=fake.get), \
             mock.patch.object(app.requests, "post", side_effect=fake.post), \
             mock.patch.object(app.requests, "patch", side_effect=fake.patch), \
             mock.patch.object(app.embedded_signup, "active_credentials", return_value=("wa-token", "123")):
            client = app.app.test_client()
            return client.post("/crm/enviar", json=body or {"lead_id": LEAD_ID, "text": "Hola, soy Rildo. ¿Te llamo a las 5?"},
                               headers={"Authorization": f"Bearer {token}", "Origin": origin})

    def test_envia_registra_y_pausa_el_bot(self):
        fake = FakeHTTP()
        r = self.enviar(fake)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://creenzia-crm.vercel.app")
        destinos = [d for d, _ in fake.posts]
        self.assertIn("graph", destinos)
        self.assertIn("crm_outbound_messages", destinos)
        guardado = [j for d, j in fake.posts if d == "conversation_messages"][0]
        self.assertTrue(guardado["message_key"].startswith("crm:"))
        self.assertEqual(guardado["direction"], "outbound")
        self.assertEqual(fake.patches[0][2], {"bot_paused": True, "bot_paused_by": "u-rildo"})

    def test_sin_sesion_valida_no_envia(self):
        fake = FakeHTTP(user_ok=False)
        r = self.enviar(fake)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(fake.posts, [])

    def test_fuera_de_las_24_horas_lo_explica(self):
        fake = FakeHTTP(graph_error=131047)
        r = self.enviar(fake)
        self.assertEqual(r.status_code, 409)
        self.assertIn("24 horas", r.get_json()["error"])
        registro = [j for d, j in fake.posts if d == "crm_outbound_messages"][0]
        self.assertEqual(registro["status"], "failed")
        self.assertFalse(any(d == "conversation_messages" for d, _ in fake.posts))
        self.assertEqual(fake.patches, [])

    def test_otro_origen_no_recibe_permiso_cors(self):
        r = self.enviar(FakeHTTP(), origin="https://otro-sitio.com")
        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_mensaje_vacio_se_rechaza(self):
        r = self.enviar(FakeHTTP(), body={"lead_id": LEAD_ID, "text": "  "})
        self.assertEqual(r.status_code, 400)


class PausaDelBotTest(unittest.TestCase):
    def test_lee_la_pausa(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        with mock.patch.object(app.requests, "get", return_value=respuesta([{"bot_paused": True}])):
            self.assertTrue(app.crm_bot_paused(memory, "51911111111"))
        with mock.patch.object(app.requests, "get", return_value=respuesta([])):
            self.assertFalse(app.crm_bot_paused(memory, "51911111111"))

    def test_si_no_se_puede_leer_el_bot_atiende(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        with mock.patch.object(app.requests, "get", side_effect=requests.ConnectionError()):
            self.assertFalse(app.crm_bot_paused(memory, "51911111111"))

    def test_en_pausa_guarda_el_mensaje_y_no_responde(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        memory.claim_work.return_value = ("claimed", "token-1")
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"from": "51911111111", "id": "wamid.p1", "type": "text", "text": {"body": "ya llegué al depa"}}]}}]}]}
        guardados = []
        with mock.patch.object(app, "valid_meta_signature", return_value=True), \
             mock.patch.object(app, "get_conversation_memory_store", return_value=memory), \
             mock.patch.object(app, "crm_bot_paused", return_value=True), \
             mock.patch.object(app.requests, "post", side_effect=lambda url, **kw: guardados.append(kw.get("json")) or respuesta()), \
             mock.patch.object(app, "send_whatsapp_message") as enviar, \
             mock.patch.object(app, "generate_reply") as generar:
            r = app.app.test_client().post("/webhook", json=payload)
        self.assertEqual(r.status_code, 200)
        enviar.assert_not_called(); generar.assert_not_called()
        self.assertEqual(guardados[0][0]["message_key"], "paused:wamid.p1")
        self.assertEqual(guardados[0][0]["content"], "ya llegué al depa")
        memory.finish_work.assert_called_once_with("wamid.p1", "token-1")


class AvisoPrivacidadTest(unittest.TestCase):
    def test_botones_validos_para_whatsapp(self):
        payload = app.consent_prompt_payload("51911111111")
        botones = payload["interactive"]["action"]["buttons"]
        self.assertEqual(len(botones), 2)
        self.assertTrue(all(len(b["reply"]["title"]) <= 20 for b in botones))
        self.assertIn(app.PRIVACY_URL, payload["interactive"]["body"]["text"])
        self.assertIn("inteligencia artificial", payload["interactive"]["body"]["text"])
        self.assertLessEqual(len(payload["interactive"]["body"]["text"]), 1024)

    def test_envia_el_aviso_una_sola_vez(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        enviados, guardados = [], []
        with mock.patch.object(app.requests, "get", return_value=respuesta([])), \
             mock.patch.object(app, "post_graph_message", side_effect=lambda p: enviados.append(p)), \
             mock.patch.object(app.requests, "post", side_effect=lambda url, **kw: guardados.append(kw["json"]) or respuesta()):
            self.assertTrue(app.send_privacy_notice(memory, "51911111111"))
        self.assertEqual(enviados[0]["type"], "interactive")
        self.assertEqual(guardados[0]["phone"], "51911111111")
        with mock.patch.object(app.requests, "get", return_value=respuesta([{"phone": "51911111111"}])), \
             mock.patch.object(app, "post_graph_message") as enviar:
            self.assertFalse(app.send_privacy_notice(memory, "51911111111"))
        enviar.assert_not_called()

    def test_si_no_se_puede_leer_no_insiste(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        with mock.patch.object(app.requests, "get", side_effect=requests.ConnectionError()), \
             mock.patch.object(app, "post_graph_message") as enviar:
            self.assertFalse(app.send_privacy_notice(memory, "51911111111"))
        enviar.assert_not_called()

    def test_respuesta_al_boton_queda_registrada(self):
        memory = mock.Mock(url="https://x.supabase.co", headers={})
        memory.claim_work.return_value = ("claimed", "tok")
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"from": "51911111111", "id": "wamid.c1", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": app.CONSENT_YES, "title": "Sí, autorizo"}}}]}}]}]}
        with mock.patch.object(app, "valid_meta_signature", return_value=True), \
             mock.patch.object(app, "get_conversation_memory_store", return_value=memory), \
             mock.patch.object(app, "send_whatsapp_message") as enviar:
            r = app.app.test_client().post("/webhook", json=payload)
        self.assertEqual(r.get_json()["processed"], 1)
        nombre, args = memory.rpc.call_args.args
        self.assertEqual(nombre, "crm_record_consent_by_phone")
        self.assertEqual(args["p_purpose"], "comercial")
        self.assertTrue(args["p_granted"])
        self.assertEqual(args["p_evidence"], "Sí, autorizo")
        self.assertIn("nuevas opciones", enviar.call_args.args[1])
        memory.finish_work.assert_called_once_with("wamid.c1", "tok")

    def test_paginas_legales_llevan_a_la_politica_de_creenzia(self):
        client = app.app.test_client()
        for ruta in ("/privacy", "/terminos", "/data-deletion"):
            r = client.get(ruta)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].startswith(app.PRIVACY_URL))

    def test_se_presenta_como_asistente_virtual_de_creenzia(self):
        self.assertIn("asistente virtual de CREENZIA Inversiones", app.INTRODUCTION)
