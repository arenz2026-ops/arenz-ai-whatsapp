import unittest
import app


class CommercialConversationTests(unittest.TestCase):
    def setUp(self):
        self.criteria = {"operation": "compra", "property_type": "departamento",
                         "districts": ["Surco"], "budget_max": 150000,
                         "currency": "USD", "bedrooms": 3, "parking_required": True}
        self.previous = {"state": {"criteria": self.criteria}}

    def observe(self, **slots):
        return {"intent": "property_search", "slot_updates": slots,
                "criteria_actions": [], "criteria_change": False, "handoff": False}

    def test_opening_is_one_question_without_numeric_menu(self):
        for observation in (None, {"intent": "greeting", "slot_updates": {}}):
            reply, _, _, _ = app.progressive_reply(None, observation, "old menu", "hola")
            self.assertIn("asistente virtual de ARENZ", reply)
            self.assertEqual(reply.count("?"), 1)
            self.assertNotIn("1️⃣", reply)

    def test_family_review_preserves_filters_without_pressure(self):
        for text in ("Lo consultaré con mis hijas y te escribo", "yo te aviso"):
            reply, criteria, stage, _ = app.progressive_reply(self.previous, self.observe(), "fallback", text)
            self.assertEqual(stage, "awaiting_customer")
            self.assertEqual(criteria, self.criteria)
            self.assertNotIn("?", reply)
            self.assertNotIn("mañana", reply)

    def test_payment_then_timing_and_no_repeated_questions(self):
        reply, criteria, _, _ = app.progressive_reply(self.previous, self.observe(), "fallback", "es correcto")
        self.assertIn("contado", reply)
        reply, criteria, _, _ = app.progressive_reply(self.previous, self.observe(payment_method="contado"), "fallback", "al contado")
        self.assertIn("cuándo", reply)
        self.assertEqual(criteria["payment_method"], "contado")
        previous = {"state": {"criteria": {**criteria, "purchase_timeline": "sin urgencia"}}}
        self.assertIsNone(app.commercial_next_question(app.conversation_state(previous)))

    def test_declining_finance_question_is_respected(self):
        criteria = {**self.criteria, "payment_method": "prefiere_no_indicar", "purchase_timeline": "prefiero no indicarlo"}
        self.assertIsNone(app.commercial_next_question(criteria))

    def test_new_commercial_fields_survive_memory_and_updates(self):
        fields = {"payment_method": "credito", "credit_status": "en_tramite",
                  "purchase_timeline": "en tres meses", "area_min_m2": 90, "bathrooms_min": 2}
        clean = app.validated_slot_updates(fields)
        self.assertEqual(app.conversation_state({"state": {"criteria": clean}}), fields)
        updated = app.apply_criteria_actions(clean, [{"action": "UPDATE", "field": "payment_method", "values": ["contado"]}])
        self.assertEqual(updated["payment_method"], "contado")
        slots = app.OBSERVATION_SCHEMA["properties"]["slot_updates"]
        self.assertEqual(set(slots["properties"]), set(slots["required"]))

    def test_matching_never_ignores_explicit_requirements(self):
        row = {"operation": "compra", "property_type": "departamento", "district": "Surco",
               "currency": "USD", "price_amount": 115000, "bedrooms": 3,
               "parking_spaces": 1, "area_total_m2": 95, "bathrooms": 2}
        criteria = {**self.criteria, "area_min_m2": 90, "bathrooms_min": 2}
        self.assertTrue(app.property_matches_criteria(row, criteria))
        for field, value in (("bedrooms", 2), ("parking_spaces", 0), ("area_total_m2", 80), ("bathrooms", None)):
            self.assertFalse(app.property_matches_criteria({**row, field: value}, criteria))

    def test_no_contact_request_overrides_automated_qualification(self):
        reply, _, stage, _ = app.progressive_reply(self.previous, self.observe(), "fallback", "No me contactes")
        self.assertEqual(stage, "do_not_contact")
        self.assertNotIn("?", reply)

    def test_multiple_ai_questions_are_rejected(self):
        self.assertIsNone(app.usable_assistant_reply({"assistant_reply": "¿Qué inmueble buscas? ¿En qué distrito?"}, "qualification"))

    def test_policy_is_in_model_request(self):
        instructions = app.build_observation_payload(None, "hola")["instructions"]
        for term in ("QUÉ", "CÓMO", "CUÁNDO", "DNI", "familia", "una sola pregunta"):
            self.assertIn(term, instructions)
