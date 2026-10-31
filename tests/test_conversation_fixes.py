import os, unittest
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app


def prev(criteria, assistant):
    return {"stage": "qualified", "state": {**criteria, "recent_turns": [{"direction": "assistant", "content": assistant}]}}


SEARCH = {"operation": "compra", "property_type": "departamento", "districts": ["Lince"], "budget_max": 100000, "currency": "USD"}
OBS = {"intent": "property_search", "slot_updates": {}, "criteria_change": False, "criteria_actions": [],
       "user_question": None, "next_action": "reply", "handoff": False, "assistant_reply": None}


class ConversationFixTests(unittest.TestCase):
    def test_bare_number_answers_bedroom_question(self):
        criteria = app.answered_count_question(prev(SEARCH, "¿Cuántos dormitorios necesitas?"), "3", dict(SEARCH))
        self.assertEqual(criteria["bedrooms"], 3)

    def test_bare_number_ignored_without_question(self):
        criteria = app.answered_count_question(prev(SEARCH, "¿En qué distrito?"), "3", dict(SEARCH))
        self.assertNotIn("bedrooms", criteria)

    def test_currency_words_are_normalized(self):
        self.assertEqual(app.validated_slot_updates({"currency": "dólares"})["currency"], "USD")
        self.assertEqual(app.validated_slot_updates({"currency": "soles"})["currency"], "PEN")

    def test_repeated_no_match_offers_advisor_instead_of_repeating(self):
        criteria = {**SEARCH, "bedrooms": 3}
        first = app.inventory_conversation_reply([], criteria)
        reply, *_ = app.progressive_reply(prev(criteria, first), OBS, "fb", "Que opciones hay", criteria, [])
        self.assertNotEqual(reply, first)
        self.assertIn("asesor", reply)
        self.assertIn("Lince", reply)

    def test_yes_after_advisor_offer_hands_off(self):
        offer = app.no_match_followup(prev(SEARCH, app.inventory_reply([])), SEARCH)
        reply, _, stage, _ = app.progressive_reply(prev(SEARCH, offer), {**OBS, "intent": "confirmation"}, "fb", "Sí", dict(SEARCH), [])
        self.assertEqual(stage, "handoff")


if __name__ == "__main__":
    unittest.main()
