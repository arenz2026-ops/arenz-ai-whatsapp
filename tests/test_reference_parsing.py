import os, unittest
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app

HANDOFF = "Perfecto. Registré tu solicitud de visita para la referencia C2-8230F7D88B3A; un asesor de C2 Inmobiliaria coordinará contigo."
OBS = {"intent": "other", "slot_updates": {}, "criteria_change": False, "criteria_actions": [],
       "user_question": None, "next_action": "reply", "handoff": False, "assistant_reply": None}


def prev(stage, assistant):
    return {"stage": stage, "state": {"recent_turns": [{"direction": "assistant", "content": assistant}]}}


class ReferenceParsingTests(unittest.TestCase):
    def test_word_referencia_is_not_a_reference(self):
        self.assertEqual(app.latest_presented_references(prev("handoff", HANDOFF)), ["C2-8230F7D88B3A"])
        self.assertEqual(app.PRESENTED_REFERENCE_PATTERN.findall("la refrigeradora y la referencia"), [])

    def test_ref_dot_still_parsed(self):
        self.assertEqual(app.PRESENTED_REFERENCE_PATTERN.findall("entrega inmediata · Ref. C2-A0A3E975EF72"), ["C2-A0A3E975EF72"])

    def test_photos_after_handoff_name_the_unit_without_reoffering(self):
        reply, _, stage, _ = app.progressive_reply(prev("handoff", HANDOFF), OBS, "fb", "Envíame fotos", {}, [])
        self.assertIn("C2-8230F7D88B3A", reply)
        self.assertNotIn("erencia.", reply.replace("referencia", ""))
        self.assertNotIn(app.ADVISOR_OFFER, reply)


if __name__ == "__main__":
    unittest.main()
