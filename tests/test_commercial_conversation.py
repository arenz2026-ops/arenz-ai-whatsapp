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
            self.assertIn("Cecilia, asesora de C2 Inmobiliaria", reply)
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

    def test_five_filters_are_available_to_advisor_and_preserve_false(self):
        criteria = {**self.criteria, "parking_required": False, "payment_method": "credito",
                    "credit_status": "en_tramite", "purchase_timeline": "sin urgencia"}
        profile = app._profile_payload("test", criteria, "mensaje", "respuesta", "qualification")
        snapshot = profile["conversation"]["qualification"]
        self.assertEqual(set(snapshot), {"que", "donde", "cuanto", "como", "cuando"})
        self.assertIs(snapshot["que"]["parking_required"], False)
        self.assertEqual(snapshot["como"]["credit_status"], "en_tramite")
        self.assertEqual(snapshot["cuando"]["purchase_timeline"], "sin urgencia")
        self.assertEqual(app.qualification_snapshot({})["como"], {})

    def test_greeting_with_saved_criteria_never_returns_inventory(self):
        for observation in (None, self.observe(), {"intent": "greeting", "slot_updates": {}}):
            for text in ("Hola", "¡Hola!", "hola 👋"):
                reply, criteria, stage, _ = app.progressive_reply(self.previous, observation, "fallback", text, inventory_matches=[])
                self.assertIn("Cecilia", reply)
                self.assertIn("retomar", reply)
                self.assertNotIn("inventario", reply)
                self.assertEqual(criteria, self.criteria)
                self.assertEqual(stage, "conversation")

    def test_explicit_budget_currency_does_not_repeat_currency_question(self):
        for text, amount, currency in (("presupuesto de 120000 dolares",120000,"USD"),
                                       ("120.000 dólares",120000,"USD"),
                                       ("120,000 USD",120000,"USD"),
                                       ("presupuesto 450000 soles",450000,"PEN")):
            previous = {"state": {"criteria": {"operation": "compra", "property_type": "departamento", "districts": ["Surco"]}}}
            observation = self.observe()
            observation["assistant_reply"] = "¿Tu presupuesto es en USD o PEN?"
            reply, criteria, _, _ = app.progressive_reply(previous, observation, "fallback", text)
            self.assertEqual(criteria["budget_max"], amount)
            self.assertEqual(criteria["currency"], currency)
            self.assertNotIn("USD o PEN", reply)
            fallback = app.with_explicit_operation(None, text)
            self.assertEqual(fallback["slot_updates"]["currency"], currency)
        self.assertIsNone(app.with_explicit_operation(None, "120000"))
        self.assertIsNone(app.with_explicit_operation(None, "USD o PEN"))

    def test_dollar_symbol_and_written_spanish_budget(self):
        for text in ("$120000", "$ 120.000", "US$120,000", "120000 $", "$120000 dólares",
                     "ciento veinte mil dólares", "ciento veinte mil dolares", "120 mil dólares"):
            observation = app.with_explicit_operation(None, text)
            self.assertEqual(observation["slot_updates"]["currency"], "USD", text)
            self.assertEqual(observation["slot_updates"]["budget_max"], 120000, text)
        self.assertEqual(app.explicit_money_amount("doscientos treinta y cinco mil dolares"), 235000)
        self.assertIsNone(app.explicit_money_amount("entre $100000 y $120000"))
        self.assertIsNone(app.with_explicit_operation(None, "$120000 o 450000 soles"))

    def test_options_distinguish_units_and_show_delivery_and_formatted_price(self):
        matches = []
        for unit, floor in (("A - 306", 3), ("A - 406", 4), ("A - 506", 5)):
            matches.append({"public": {"public_reference": "C2-" + str(floor), "property_type": "departamento",
                "district": "Chorrillos", "currency": "PEN", "price_amount": 250000.0,
                "bedrooms": 2, "bathrooms": None, "area_total_m2": 52.34, "parking_spaces": None,
                "provenance": "partner", "media": [],
                "public_description": f"Proyecto CONDOMINIO LOS BOSQUES; unidad {unit}. Piso {floor}. Entrega según fuente: 1er Tri 2029."}})
        reply = app.inventory_reply(matches)
        for unit in ("A - 306", "A - 406", "A - 506"):
            self.assertIn(unit, reply)
        self.assertEqual(reply.count("S/ 250.000"), 3)
        self.assertIn("52,34 m²", reply)
        self.assertIn("1er Tri 2029", reply)
        self.assertIn("estacionamiento por confirmar", reply)
        self.assertNotIn("250000.0", reply)
        self.assertNotIn("None", reply)

    def test_inventory_always_continues_for_search_and_general_question(self):
        public = {"public_reference": "C2-TEST", "property_type": "departamento", "district": "Surco",
            "currency": "USD", "price_amount": 120000, "bedrooms": 3, "parking_spaces": 1,
            "media": [], "provenance": "partner", "public_description": "Proyecto TEST; unidad 306. Entrega según fuente: 1er Tri 2029."}
        matches = [{"public": public}]
        for intent in ("property_search", "general_question"):
            observation = self.observe()
            observation["intent"] = intent
            reply, criteria, _, _ = app.progressive_reply(self.previous, observation, "fallback", "muéstrame opciones", inventory_matches=matches)
            self.assertTrue(reply.endswith("¿Para cuándo necesitas tener el departamento?"))
            self.assertIn("1er Tri 2029", reply)
            self.assertEqual(reply.count("?"), 1)
            known = {**self.criteria, "payment_method": "contado", "purchase_timeline": "sin urgencia"}
            previous = {"state": {"criteria": known}}
            reply, _, _, _ = app.progressive_reply(previous, observation, "fallback", "muéstrame opciones", inventory_matches=matches)
            self.assertTrue(reply.endswith("¿Cuál de estas unidades te gustaría revisar con más detalle?"))
            self.assertNotIn("¿Para cuándo", reply)

    def test_long_inventory_context_keeps_the_final_question_and_references(self):
        text = "Ref. C2-A\n" + "detalle " * 100 + "Ref. C2-B\n¿Para cuándo necesitas tener el departamento?"
        context = app.bounded_turn_content(text, "assistant")
        self.assertLessEqual(len(context), 280)
        self.assertIn("Ref. C2-A", context)
        self.assertIn("Ref. C2-B", context)
        self.assertTrue(context.endswith("¿Para cuándo necesitas tener el departamento?"))

    def test_number_selects_the_displayed_unit_instead_of_repeating_results(self):
        def item(ref, unit):
            return {"public": {"public_reference": ref, "property_type": "departamento", "district": "Surco",
                "currency": "USD", "price_amount": 120000, "bedrooms": 3, "parking_spaces": 1,
                "media": [], "provenance": "partner", "public_description": f"Proyecto TEST; unidad {unit}."}}
        first, second = item("C2-FIRST", "406"), item("C2-SECOND", "506")
        criteria = {**self.criteria, "payment_method": "contado", "purchase_timeline": "sin urgencia"}
        opening = app.inventory_conversation_reply([first, second], criteria)
        previous = {"stage": "qualified", "state": {"active_search_id": "search-test", "searches": {
            "search-test": {"criteria": criteria, "recent_turns": [{"direction": "assistant", "content": opening}]}}}}
        for text in ("1", "el 1", "opción 1", "la primera"):
            observation = self.observe(bedrooms=1)
            reply, retained, stage, _ = app.progressive_reply(previous, observation, "fallback", text, inventory_matches=[second, first])
            self.assertIn("C2-FIRST", reply)
            self.assertNotIn("C2-SECOND", reply)
            self.assertEqual(retained["bedrooms"], 3)
            self.assertEqual(stage, "property_detail")
        reply, _, _, _ = app.progressive_reply(previous, self.observe(), "fallback", "9", inventory_matches=[first, second])
        self.assertIn("Te mostré 2 opciones", reply)
        reply, _, _, _ = app.progressive_reply(previous, self.observe(), "fallback", "1", inventory_matches=[second])
        self.assertIn("No pude reconfirmar", reply)
        self.assertNotIn("Unidad 506", reply)
        previous["state"]["searches"]["search-test"]["recent_turns"].append({"direction": "assistant", "content": "¿Cuántos dormitorios necesitas?"})
        self.assertIsNone(app.selected_option(previous, "1"))

    def test_recent_context_comes_from_active_search_not_an_old_session(self):
        previous = {"state": {"active_search_id": "new", "recent_turns": [{"direction": "assistant", "content": "viejo"}],
            "searches": {"new": {"recent_turns": [{"direction": "assistant", "content": "actual"}]}}}}
        self.assertEqual(app.recent_conversation_turns(previous)[0]["content"], "actual")
