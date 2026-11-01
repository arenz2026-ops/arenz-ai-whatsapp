import unittest
from unittest import mock

import requests

import app

URLS = [f"https://x/{i}.jpg" for i in range(1, 13)]


def store(sent=()):
    s = mock.Mock()
    s.photos_for_reference.return_value = URLS
    s.sent_media.side_effect = lambda phone, urls: {u for u in urls if u in set(sent)}
    return s


def history(*assistant_messages):
    return {"recent_messages": [{"direction": "assistant", "content": m} for m in assistant_messages]}


class PhotoTurnTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app, "recent_conversation_turns",
                                    side_effect=lambda previous, limit=8, raw=False: (previous or {}).get("recent_messages", []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def store(self, sent=()):
        s = store(sent)
        s.closing_question.return_value = "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"
        return s

    def test_listing_sends_no_photos(self):
        listing = "Opciones:\n1. Libertador\nRef. C2-AAA111\n2. Otra\nRef. C2-BBB222\nResponde con el número"
        plan = app.photo_turn(self.store(), "51", history("¿Para cuándo?"), "sí", listing, "qualified")
        self.assertEqual(plan, {"stored": listing, "text": listing, "photos": []})

    def test_choosing_unit_ficha_first_then_cover_with_offer(self):
        previous = history("Opciones:\n1. Libertador\nRef. C2-AAA111\n2. Otra\nRef. C2-BBB222\nResponde con el número de la opción")
        ficha = "🏠 Ficha\n\n¿Quieres que un asesor te envíe los planos y el brochure del proyecto?\n\nRef. C2-AAA111"
        with mock.patch.object(app, "selected_option", return_value={"index": 1, "reference": "C2-AAA111", "count": 2}):
            plan = app.photo_turn(self.store(), "51", previous, "1", ficha, "property_detail")
        self.assertNotIn("brochure", plan["text"])
        self.assertEqual([p["url"] for p in plan["photos"]], [URLS[0]])
        self.assertEqual(plan["photos"][0]["caption"], "📷 Tengo 11 fotos más de este proyecto. ¿Quieres que te envíe más fotos?")
        self.assertIn(app.MORE_PHOTOS_QUESTION, plan["stored"])
        self.assertNotIn("C2-AAA111", app.customer_visible_reply(plan["text"]))

    def test_yes_sends_next_five_question_on_last_photo(self):
        previous = history("Ficha\n📷 Tengo 11 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        plan = app.photo_turn(self.store(sent=URLS[:1]), "51", previous, "Sí", "otra cosa", "property_detail")
        self.assertIsNone(plan["text"])
        self.assertEqual([p["url"] for p in plan["photos"]], URLS[1:6])
        self.assertNotIn("caption", plan["photos"][3])
        self.assertIn("Tengo 6 fotos más", plan["photos"][-1]["caption"])

    def test_last_batch_ends_with_brochure_question(self):
        previous = history("📷 Tengo 1 foto más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        plan = app.photo_turn(self.store(sent=URLS[:11]), "51", previous, "dale", "x", "property_detail")
        self.assertEqual([p["url"] for p in plan["photos"]], URLS[11:])
        caption = plan["photos"][-1]["caption"]
        self.assertTrue(caption.startswith("Esas son todas las fotos"))
        self.assertIn("brochure", caption)

    def test_nothing_left(self):
        previous = history("📷 Tengo 1 foto más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        plan = app.photo_turn(self.store(sent=URLS), "51", previous, "sí", "x", "handoff")
        self.assertEqual(plan["photos"], [])
        self.assertIn("Ya te envié todas", plan["text"])

    def test_yes_without_offer_does_nothing(self):
        previous = history("Ficha sin oferta\nRef. C2-AAA111")
        plan = app.photo_turn(self.store(), "51", previous, "sí", "respuesta normal", "property_detail")
        self.assertEqual(plan["photos"], [])
        self.assertEqual(plan["text"], "respuesta normal")

    def test_listing_cta_mentions_photos(self):
        import inspect
        self.assertIn("ficha completa y fotografías del proyecto", inspect.getsource(app))


class SendPhotosTest(unittest.TestCase):
    def test_skips_already_sent_and_records_new(self):
        s = mock.Mock()
        s.sent_media.return_value = {"https://x/1.jpg"}
        with mock.patch.object(app, "post_graph_message") as post:
            sent = app.send_property_photos("51999", [{"url": "https://x/1.jpg"}, {"url": "https://x/2.jpg", "reference": "C2-B"}], s)
        self.assertEqual(sent, 1)
        self.assertEqual(post.call_args[0][0]["image"], {"link": "https://x/2.jpg"})
        s.record_media_delivery.assert_called_once_with("51999", "C2-B", "https://x/2.jpg")

    def test_graph_failure_never_raises(self):
        with mock.patch.object(app, "post_graph_message", side_effect=requests.ConnectionError()):
            self.assertEqual(app.send_property_photos("51999", [{"url": "https://x/1.jpg"}], None), 0)

    def test_record_failure_still_counts_as_sent(self):
        s = mock.Mock()
        s.sent_media.return_value = set()
        s.record_media_delivery.side_effect = requests.HTTPError("409")
        with mock.patch.object(app, "post_graph_message"):
            self.assertEqual(app.send_property_photos("51999", [{"url": "https://x/1.jpg"}], s), 1)


if __name__ == "__main__":
    unittest.main()


class DeclineMorePhotosTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app, "recent_conversation_turns",
                                    side_effect=lambda previous, limit=8, raw=False: (previous or {}).get("recent_messages", []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_to_more_photos_asks_closing_question(self):
        s = store(sent=URLS[:6])
        s.closing_question.return_value = "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"
        previous = history("📷 Tengo 6 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        plan = app.photo_turn(s, "51", previous, "No", "descripción repetida", "property_detail")
        self.assertEqual(plan["photos"], [])
        self.assertEqual(plan["text"], "Perfecto. ¿Quieres que un asesor te envíe los planos y el brochure del proyecto?")
        # A following "No" then closes the conversation.
        closing = app.declined_offer_reply(history(plan["stored"]), "No")
        self.assertTrue(closing.startswith("Entendido, no te contactará un asesor"))
