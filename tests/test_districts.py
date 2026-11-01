import os, unittest
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app


class DistrictTests(unittest.TestCase):
    def test_accent_and_alias_variants_are_canonical(self):
        for raw in ("Jesús Maria", "jesus maria", "Jesús María"):
            self.assertEqual(app.validated_slot_updates({"districts": [raw]})["districts"], ["Jesús María"])
        self.assertEqual(app.validated_slot_updates({"districts": ["Santiago de Surco"]})["districts"], ["Surco"])

    def test_inventory_spelling_without_accent_still_matches(self):
        row = {"operation": "compra", "district": "Jesús Maria", "property_type": "departamento", "bedrooms": 2,
               "currency": "USD", "price_amount": 150000, "parking_spaces": 1}
        criteria = {"operation": "compra", "districts": ["Jesús María"], "property_type": "departamento",
                    "budget_max": 200000, "currency": "USD", "bedrooms": 2}
        self.assertTrue(app.property_matches_criteria(row, criteria))
        self.assertIn("Jesús Maria", app.district_variants("Jesús María"))


if __name__ == "__main__":
    unittest.main()
