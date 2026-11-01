import unittest
from unittest import mock

import app


def history(last):
    return {"recent_messages": [{"direction": "assistant", "content": last}]}


class DeclinedOfferTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app, "recent_conversation_turns",
                                    side_effect=lambda previous, limit=8, raw=False: (previous or {}).get("recent_messages", []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_after_brochure_offer_closes(self):
        last = "Esas son todas las fotos que tengo de este proyecto. ¿Quieres que un asesor te envíe los planos y el brochure del proyecto?\nRef. C2-A"
        for answer in ("No", "no gracias", "Por ahora no", "No, gracias.", "no quiero"):
            reply = app.declined_offer_reply(history(last), answer)
            self.assertTrue(reply and reply.startswith("Entendido, no te contactará un asesor"), answer)

    def test_no_after_visit_or_advisor_offer_closes(self):
        self.assertTrue(app.declined_offer_reply(history("¿Quieres que coordinemos una visita al departamento?"), "no"))
        self.assertTrue(app.declined_offer_reply(history("¿Quieres que te contacte un asesor?"), "no"))

    def test_no_with_new_request_keeps_conversation(self):
        last = "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"
        self.assertIsNone(app.declined_offer_reply(history(last), "no, pero muéstrame otra opción"))

    def test_no_without_offer_is_not_closing(self):
        self.assertIsNone(app.declined_offer_reply(history("¿Cuántos dormitorios necesitas?"), "no"))

    def test_thanks_after_closing(self):
        closing = app.OFFER_DECLINED_REPLY.format(brand=app.COMMERCIAL_BRAND)
        self.assertEqual(app.declined_offer_reply(history(closing), "ok gracias"), app.CLOSING_THANKS_REPLY)

    def test_progressive_reply_uses_closing(self):
        last = "Esas son todas las fotos que tengo de este proyecto. ¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"
        reply, _, stage, _ = app.progressive_reply(history(last), {"intent": "other"}, "fallback", "No")
        self.assertTrue(reply.startswith("Entendido, no te contactará un asesor"))
        self.assertEqual(stage, "awaiting_customer")


if __name__ == "__main__":
    unittest.main()
