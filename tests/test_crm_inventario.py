"""Inventario C2 como programa (crm/inventario.html): lectura del inventario y ficha comercial para el equipo."""
import unittest
from unittest import mock

import requests

import app

FILA = {"property_id": "p1", "public_reference": "C2-8230F7D88B3A", "operation": "compra", "property_type": "departamento",
        "district": "Surco", "zone": None, "public_location_reference": None, "price_amount": 420000, "currency": "PEN",
        "bedrooms": 3, "bathrooms": 2, "area_m2": 74.58, "area_total_m2": 74.58, "area_built_m2": None,
        "terrace_area_m2": None, "parking_spaces": 1, "features": [], "lifecycle_state": "active_confirmed",
        "public_description": "Proyecto Santo Cristo; unidad 304. Piso 3. Vista INTERNA. Entrega según fuente: Entregado. "
                              "Estacionamiento no confirmado como incluido en el precio.",
        "availability_confirmed_at": "2026-11-16T17:30:58+00:00", "approved_at": "2026-10-22T00:00:00+00:00",
        "provenance": "partner", "verification_status": "verified", "collaboration_confirmed_at": None,
        "source_name": "lista_precios"}


def respuesta(json=None, status=200):
    r = mock.Mock()
    r.json.return_value = json if json is not None else []
    r.status_code = status
    r.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(response=r)
    return r


class InventarioCrmTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.memory = mock.Mock(url="https://x.supabase.co", headers={"apikey": "k"})
        self.store = mock.Mock(url="https://x.supabase.co", headers={"apikey": "k"})

    def post(self, path, **kw):
        return self.client.post(path, headers={"Authorization": "Bearer t", "Origin": app.CRM_ORIGIN}, **kw)

    def test_sin_sesion_no_entrega_datos(self):
        with mock.patch.object(app, "get_conversation_memory_store", return_value=self.memory), \
             mock.patch.object(app, "crm_user_from_token", return_value=None), \
             mock.patch.object(app.requests, "get") as get:
            r = self.post("/crm/inventario")
        self.assertEqual(r.status_code, 403)
        get.assert_not_called()

    def test_lee_todas_las_paginas(self):
        pagina = [{"ref": f"C2-{i:012X}"} for i in range(app.INVENTARIO_PAGE)]
        with mock.patch.object(app, "get_conversation_memory_store", return_value=self.memory), \
             mock.patch.object(app, "crm_user_from_token", return_value={"id": "u"}), \
             mock.patch.object(app.requests, "get", side_effect=[respuesta(pagina), respuesta([{"ref": "C2-X"}])]) as get:
            r = self.post("/crm/inventario")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["rows"]), app.INVENTARIO_PAGE + 1)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["offset"], app.INVENTARIO_PAGE)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), app.CRM_ORIGIN)

    def test_ficha_es_la_del_bot(self):
        with mock.patch.object(app, "get_conversation_memory_store", return_value=self.memory), \
             mock.patch.object(app, "get_inventory_store", return_value=self.store), \
             mock.patch.object(app, "crm_user_from_token", return_value={"id": "u"}), \
             mock.patch.object(app.requests, "get", return_value=respuesta([FILA])), \
             mock.patch.object(app, "full_unit_reply", return_value="FICHA DEL BOT") as ficha:
            r = self.post("/crm/ficha", json={"ref": "C2-8230F7D88B3A"})
        self.assertEqual(r.get_json()["text"], "FICHA DEL BOT")
        item = ficha.call_args.args[0]
        self.assertEqual(item["public"]["public_reference"], "C2-8230F7D88B3A")
        self.assertEqual(item["delivery"], "inmediata")

    def test_ficha_rechaza_referencias_raras(self):
        with mock.patch.object(app, "get_conversation_memory_store", return_value=self.memory), \
             mock.patch.object(app, "get_inventory_store", return_value=self.store), \
             mock.patch.object(app.requests, "get") as get:
            r = self.post("/crm/ficha", json={"ref": "C2-1' or 1=1"})
        self.assertEqual(r.status_code, 400)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
