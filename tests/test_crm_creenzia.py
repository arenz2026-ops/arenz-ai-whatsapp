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
