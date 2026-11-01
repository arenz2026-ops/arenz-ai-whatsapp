import os, unittest
from unittest import mock
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app

ITEM = {"public": {"public_reference": "C2-X", "district": "Surco", "property_type": "departamento",
                   "price_amount": 500000, "currency": "PEN", "bedrooms": 3, "bathrooms": 2, "area_total_m2": 100,
                   "parking_spaces": 1, "public_description": "Proyecto Santo Cristo; unidad 304. Piso 3. Entrega según fuente: Entregado."}}


class FakeStore:
    def __init__(self, notice):
        self.notice = notice

    def unit_notice(self, reference):
        return self.notice

    def project_card(self, reference):
        return "📍 DIRECCIÓN: Calle 1\n⚠️ Tiene un embargo en etapa de saneamiento\n🛏️ 3 habitaciones"


class UnitNoticeTests(unittest.TestCase):
    def test_charge_notice_goes_last_in_general_terms(self):
        with mock.patch.object(app, "get_inventory_store", return_value=FakeStore("carga")), \
             mock.patch.object(app, "ai_text", return_value="🏠 FICHA\n¿Quieres que coordinemos una visita al departamento?"):
            reply = app.full_unit_reply(ITEM)
        self.assertTrue(reply.endswith(app.CHARGE_NOTICE))
        self.assertIn("asesor", reply.rsplit("\n\n", 1)[1])
        self.assertNotIn("embargo", reply.casefold())

    def test_no_notice_without_flag(self):
        with mock.patch.object(app, "get_inventory_store", return_value=FakeStore("")), \
             mock.patch.object(app, "ai_text", return_value="🏠 FICHA"):
            reply = app.full_unit_reply(ITEM)
        self.assertNotIn("carga", reply)

    def test_embargo_line_removed_from_card_sent_to_model(self):
        self.assertNotIn("embargo", app.clean_card_text(FakeStore("").project_card("x")).casefold())


if __name__ == "__main__":
    unittest.main()
