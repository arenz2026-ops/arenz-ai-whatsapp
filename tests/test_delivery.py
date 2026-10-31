import os, unittest
from datetime import date
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app

TODAY = date(2026, 10, 31)


def row(label, source="lista_precios"):
    desc = f"Proyecto X; unidad 1. Entrega según fuente: {label}." if label else "Proyecto X; unidad 1."
    return {"public_description": desc, "source_name": source}


def item(ref, label):
    return {"public": {"public_reference": ref, "property_type": "departamento", "district": "Lince", "currency": "PEN",
                       "price_amount": 300000, "bedrooms": 3, "media": [], "provenance": "partner",
                       "public_description": f"Proyecto X; unidad {ref}. Entrega según fuente: {label}."},
            "delivery": app.property_delivery(row(label), TODAY)}


CRITERIA = {"operation": "compra", "property_type": "departamento", "districts": ["Lince"], "budget_max": 100000,
            "currency": "USD", "bedrooms": 3, "payment_method": "credito", "purchase_timeline": "por definir"}


class DeliveryTests(unittest.TestCase):
    def test_labels_classify(self):
        self.assertEqual(app.property_delivery(row("Entregado"), TODAY), "inmediata")
        self.assertEqual(app.property_delivery(row("Sept 2026"), TODAY), "inmediata")
        self.assertEqual(app.property_delivery(row("Oct-27"), TODAY), "futura")
        self.assertEqual(app.property_delivery(row("1er Tri 2029"), TODAY), "futura")
        self.assertEqual(app.property_delivery(row("X Confirmar"), TODAY), "por_confirmar")
        self.assertEqual(app.property_delivery(row(None, "trello"), TODAY), "inmediata")
        self.assertEqual(app.property_delivery(row(None, "power_bi_tale"), TODAY), "por_confirmar")

    def test_both_buckets_ask_which_before_listing(self):
        matches = [item("A", "Entregado"), item("B", "1er Tri 2029")]
        reply, _ = app.delivery_reply(None, matches, CRITERIA, "ok")
        self.assertIn("1 de entrega inmediata", reply)
        self.assertIn(app.DELIVERY_QUESTION, reply)
        self.assertNotIn("Ref.", reply)

    def test_explicit_choice_lists_only_that_bucket(self):
        matches = [item("A", "Entregado"), item("B", "1er Tri 2029")]
        reply, criteria = app.delivery_reply(None, matches, CRITERIA, "entrega inmediata")
        self.assertIn("Ref. A", reply); self.assertNotIn("Ref. B", reply)
        self.assertEqual(criteria["delivery_preference"], "inmediata")

    def test_urgent_timeline_goes_straight_to_immediate(self):
        matches = [item("A", "Entregado"), item("B", "1er Tri 2029")]
        reply, _ = app.delivery_reply(None, matches, {**CRITERIA, "purchase_timeline": "lo antes posible"}, "contado")
        self.assertIn("Ref. A", reply); self.assertNotIn("Ref. B", reply)


if __name__ == "__main__":
    unittest.main()
