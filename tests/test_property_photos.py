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

    def test_listing_sends_no_photos(self):
        listing = "Opciones:\n1. Libertador\nRef. C2-AAA111\n2. Otra\nRef. C2-BBB222\nResponde con el número"
        reply, photos, first = app.photo_turn(store(), "51", history("¿Para cuándo?"), "sí", listing, "qualified")
        self.assertEqual((reply, photos, first), (listing, [], False))

    def test_choosing_unit_sends_cover_and_offers_more(self):
        previous = history("Opciones:\n1. Libertador\nRef. C2-AAA111\n2. Otra\nRef. C2-BBB222\nResponde con el número de la opción")
        with mock.patch.object(app, "selected_option", return_value={"index": 1, "reference": "C2-AAA111", "count": 2}):
            reply, photos, first = app.photo_turn(store(), "51", previous, "1", "Ficha de la unidad", "property_detail")
        self.assertEqual([p["url"] for p in photos], [URLS[0]])
        self.assertTrue(first)
        self.assertIn("Tengo 11 fotos más", reply)
        self.assertIn(app.MORE_PHOTOS_QUESTION, reply)
        self.assertNotIn("C2-AAA111", app.customer_visible_reply(reply))

    def test_yes_sends_next_five_and_keeps_offering(self):
        previous = history("Ficha\n📷 Tengo 11 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        reply, photos, first = app.photo_turn(store(sent=URLS[:1]), "51", previous, "Sí", "otra cosa", "property_detail")
        self.assertEqual([p["url"] for p in photos], URLS[1:6])
        self.assertIn("Tengo 6 fotos más", reply)

    def test_last_batch_closes_with_next_step(self):
        previous = history("📷 Tengo 6 fotos más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        reply, photos, _ = app.photo_turn(store(sent=URLS[:6]), "51", previous, "dale", "x", "property_detail")
        self.assertEqual(len(photos), 5)
        self.assertIn("Tengo 1 foto más", reply)
        reply, photos, _ = app.photo_turn(store(sent=URLS[:11]), "51", previous, "sí", "x", "property_detail")
        self.assertEqual([p["url"] for p in photos], URLS[11:])
        self.assertIn("Esas son todas", reply)
        self.assertNotIn(app.MORE_PHOTOS_QUESTION, reply)

    def test_nothing_left(self):
        previous = history("📷 Tengo 1 foto más de este proyecto. ¿Quieres que te envíe más fotos?\nRef. C2-AAA111")
        reply, photos, _ = app.photo_turn(store(sent=URLS), "51", previous, "sí", "x", "handoff")
        self.assertEqual(photos, [])
        self.assertIn("Ya te envié todas", reply)

    def test_yes_without_offer_does_nothing(self):
        previous = history("Ficha sin oferta\nRef. C2-AAA111")
        reply, photos, _ = app.photo_turn(store(), "51", previous, "sí", "respuesta normal", "property_detail")
        self.assertEqual((reply, photos), ("respuesta normal", []))


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
