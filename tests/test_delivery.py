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


class TimelineAndUnitTests(unittest.TestCase):
    def test_month_means_immediate_and_future_year_means_future(self):
        self.assertEqual(app.delivery_preference_from_text("Dentro del mes de noviembre"), "inmediata")
        self.assertEqual(app.delivery_preference_from_text("para diciembre de 2028"), "futura")

    def test_location_question_does_not_resend_the_card(self):
        it = item("607", "Abril 2028")
        it["public"]["public_description"] = "Proyecto Unico Canevaro; unidad 607. Piso 6. Entrega según fuente: Abril 2028."
        card = app.selected_unit_reply(it)
        previous = {"stage": "property_detail", "state": {"recent_turns": [{"direction": "assistant", "content": card}]}}
        obs = {"intent": "general_question", "slot_updates": {}, "criteria_change": False, "criteria_actions": [],
               "user_question": "ubicación", "next_action": "reply", "handoff": False, "assistant_reply": None}
        reply = app.unit_question_reply(previous, it, "Cerca a qué lugares está?", obs)
        self.assertIn("Lince", reply); self.assertIn("te contacte un asesor", reply)
        self.assertNotEqual(reply, card)
        self.assertNotIn("visita", reply)
        self.assertIn("planos", reply)
        ready = item("A", "Entregado")
        self.assertIn("visita", app.unit_question_reply(previous, ready, "dónde está", obs))


if __name__ == "__main__":
    unittest.main()


class FichaTests(unittest.TestCase):
    def test_card_header_with_company_and_contacts_is_dropped(self):
        card = "# INFO\n🔴EMPRESA DESARROLLADORA\n- Real Inmobiliaria SAC\n🔴NÚMERO DE CONTACTO DE VISITAS.\n- Danna\n📍DIRECCIÓN: Ca. Los Tulipanes 250, Lince"
        cleaned = app.clean_card_text(card)
        self.assertNotIn("Real Inmobiliaria", cleaned); self.assertNotIn("Danna", cleaned)
        self.assertIn("Tulipanes", cleaned)

    def test_full_ficha_keeps_reference_and_falls_back(self):
        it = item("C2-X1", "Abril 2028")
        from unittest.mock import patch
        with patch("app.project_card_text", return_value=""), patch("app.ai_text", return_value="🏠 DEPARTAMENTO EN VENTA"):
            self.assertTrue(app.full_unit_reply(it).endswith("Ref. C2-X1"))
        with patch("app.project_card_text", return_value=""), patch("app.ai_text", return_value=None):
            self.assertIn("Revisemos esta unidad", app.full_unit_reply(it))


class SaleOnlyFlowTests(unittest.TestCase):
    OBS = {"intent": "property_search", "slot_updates": {}, "criteria_change": False, "criteria_actions": [],
           "user_question": None, "next_action": "reply", "handoff": False, "assistant_reply": None}

    def test_never_asks_operation_or_type(self):
        reply, *_ = app.progressive_reply(None, self.OBS, "fb", "quiero un depa")
        self.assertNotIn("alquilar", reply); self.assertIn("distrito", reply)

    def test_budget_question_mentions_both_currencies(self):
        obs = {**self.OBS, "slot_updates": {"districts": ["Lince"]}}
        reply, *_ = app.progressive_reply(None, obs, "fb", "Lince")
        self.assertIn("soles o en dólares", reply)

    def test_rental_request_is_redirected(self):
        obs = {**self.OBS, "slot_updates": {"operation": "alquiler"}}
        reply, criteria, *_ = app.progressive_reply(None, obs, "fb", "quiero alquilar")
        self.assertIn("solo ofrecemos departamentos en venta", reply)
        self.assertNotIn("operation", criteria)

    def test_payment_and_timeline_questions_are_worded_differently(self):
        matches = [item("A", "Entregado")]
        first, _ = app.qualified_inventory_reply(None, matches, dict(CRITERIA, payment_method=None, purchase_timeline=None), "3")
        prev = {"state": {"recent_turns": [{"direction": "assistant", "content": first}]}}
        second, _ = app.qualified_inventory_reply(prev, matches, dict(CRITERIA, payment_method="credito", purchase_timeline=None), "crédito")
        self.assertIn("pagarlo", first); self.assertIn("Último dato", second)
        self.assertNotIn("Tengo opciones", second)

    def test_more_shows_the_next_units(self):
        matches = [item(f"R{i}", "1er Tri 2029") for i in range(1, 8)]
        first, crit = app.delivery_reply(None, matches, CRITERIA, "ok")
        prev = {"state": {"recent_turns": [{"direction": "assistant", "content": first}]}}
        more, _ = app.delivery_reply(prev, matches, crit, "muéstrame las otras")
        self.assertIn("Ref. R4", more); self.assertNotIn("Ref. R1 ", more)
        self.assertIn("4.", more)


class ResumeAnswerTests(unittest.TestCase):
    def test_no_after_resume_question_starts_new_search(self):
        prev = {"state": {"recent_turns": [{"direction": "assistant", "content": "Hola, soy Cecilia. " + app.RESUME_QUESTION}]}}
        self.assertEqual(app.resume_answer_as_command(prev, "No"), "nueva búsqueda")
        self.assertEqual(app.resume_answer_as_command(prev, "Otra búsqueda"), "nueva búsqueda")
        self.assertEqual(app.resume_answer_as_command(prev, "Retomar"), "Retomar")
        self.assertEqual(app.resume_answer_as_command(None, "No"), "No")


class SelectionAfterListingTests(unittest.TestCase):
    def listing_prev(self, refs, extra=None):
        listing = "Te muestro:\n" + "\n".join(f"{i}. X · Lince\nS/ 1 · Ref. {r}" for i, r in enumerate(refs, 1)) + "\n\nResponde con el número de la opción que te interesa."
        turns = [{"direction": "assistant", "content": listing}]
        if extra:
            turns += [{"direction": "user", "content": "si"}, {"direction": "assistant", "content": extra}]
        return {"stage": "qualified", "state": {"recent_turns": turns}}

    def test_yes_with_single_option_selects_it(self):
        sel = app.selected_option(self.listing_prev(["C2-AAA"]), "Si")
        self.assertEqual(sel["reference"], "C2-AAA")

    def test_free_text_number_selects_even_after_advisor_note(self):
        prev = self.listing_prev(["C2-AAA", "C2-BBB"], "Perfecto. Registraré tu solicitud para que un asesor continúe contigo.")
        self.assertEqual(app.selected_option(prev, "Dame la Info del departamento 2")["reference"], "C2-BBB")

    def test_bedroom_count_is_not_a_selection(self):
        self.assertIsNone(app.selected_option(self.listing_prev(["C2-AAA"]), "3 dormitorios"))

    def test_model_handoff_flag_alone_does_not_hand_off(self):
        obs = {"intent": "general_question", "handoff": True, "slot_updates": {}}
        self.assertIsNone(app.detected_handoff(None, obs, "Que pasó"))


class FichaFollowUpTests(unittest.TestCase):
    def prev_with_ficha(self, closing):
        ficha = f"🏠 DEPARTAMENTO EN VENTA – UNIVERSO\n...\n{closing}\n\nRef. C2-D5ACE8F77103"
        return {"stage": "property_detail", "state": {"recent_turns": [{"direction": "assistant", "content": ficha}]}}

    def test_yes_after_visit_offer_hands_off_a_visit(self):
        prev = self.prev_with_ficha("¿Quieres que coordinemos una visita al departamento?")
        self.assertIsNone(app.selected_option(prev, "Si"))
        h = app.detected_handoff(prev, {"intent": "confirmation", "handoff": False}, "Si")
        self.assertEqual((h["request_type"], h["property_reference"]), ("visit", "C2-D5ACE8F77103"))

    def test_yes_after_brochure_offer_hands_off_interest(self):
        prev = self.prev_with_ficha("¿Quieres que un asesor te envíe los planos y el brochure del proyecto?")
        self.assertEqual(app.detected_handoff(prev, {"handoff": False}, "sí")["request_type"], "property_interest")


class PagingSelectionTests(unittest.TestCase):
    def test_number_on_second_page_maps_to_its_reference(self):
        matches = [item(f"R{i}", "1er Tri 2029") for i in range(1, 8)]
        first, crit = app.delivery_reply(None, matches, CRITERIA, "ok")
        prev = {"state": {"recent_turns": [{"direction": "assistant", "content": first}]}}
        more, _ = app.delivery_reply(prev, matches, crit, "muéstrame los otros")
        prev2 = {"state": {"recent_turns": [{"direction": "assistant", "content": first}, {"direction": "user", "content": "otros"},
                                            {"direction": "assistant", "content": more}]}}
        self.assertEqual(app.selected_option(prev2, "4")["reference"], "R4")

    def test_otros_is_a_more_request_but_otra_busqueda_is_not(self):
        self.assertTrue(app.is_more_request("Muestrame los otros"))
        self.assertTrue(app.is_more_request("Quiero que me muestres otro departamento"))
        self.assertFalse(app.is_more_request("otra búsqueda"))
