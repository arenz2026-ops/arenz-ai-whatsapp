import unittest
from unittest import mock

import requests

import app


def item(reference, url=None, project="Libertador", district="Pueblo Libre"):
    media = [{"media_type": "photo", "media_url": url}] if url else []
    return {"public": {"public_reference": reference, "district": district,
                       "public_description": f"Proyecto {project}; unidad 501.", "media": media}}


class ListingPhotosTest(unittest.TestCase):
    def test_only_listed_units_with_photos_one_per_image(self):
        matches = [item("C2-A", "https://x/1.jpg"), item("C2-B", "https://x/1.jpg"),
                   item("C2-C", "https://x/2.jpg"), item("C2-D", "https://x/3.jpg")]
        reply = "Opciones:\n1. A\nRef. C2-A\n2. B\nRef. C2-B\n3. C\nRef. C2-C"
        photos = app.listing_photos(reply, matches)
        self.assertEqual([p["url"] for p in photos], ["https://x/1.jpg", "https://x/2.jpg"])
        self.assertEqual(photos[0]["caption"], "Opción 1: Libertador · Pueblo Libre (imagen referencial)")

    def test_no_photo_line_in_listing_text(self):
        matches = [{"public": {**item("C2-A", "https://x/1.jpg")["public"], "operation": "compra", "property_type": "departamento",
                               "price_amount": 100000, "currency": "USD", "bedrooms": 2, "bathrooms": 2, "provenance": "partner",
                               "parking_spaces": None}}]
        self.assertNotIn("https://x/1.jpg", app.inventory_reply(matches))

    def test_no_matches_no_photos(self):
        self.assertEqual(app.listing_photos("Ref. C2-A", None), [])


class SendPhotosTest(unittest.TestCase):
    def test_skips_already_sent_and_records_new(self):
        store = mock.Mock()
        store.sent_media.return_value = {"https://x/1.jpg"}
        with mock.patch.object(app, "post_graph_message") as post:
            sent = app.send_property_photos("51999", [{"url": "https://x/1.jpg"}, {"url": "https://x/2.jpg", "reference": "C2-B"}], store)
        self.assertEqual(sent, 1)
        self.assertEqual(post.call_args[0][0]["image"], {"link": "https://x/2.jpg"})
        store.record_media_delivery.assert_called_once_with("51999", "C2-B", "https://x/2.jpg")

    def test_graph_failure_never_raises(self):
        with mock.patch.object(app, "post_graph_message", side_effect=requests.ConnectionError()):
            self.assertEqual(app.send_property_photos("51999", [{"url": "https://x/1.jpg"}], None), 0)

    def test_requested_reply_by_stage(self):
        self.assertIn("visita", app.requested_photos_reply("property_detail"))
        self.assertNotIn("visita", app.requested_photos_reply("handoff"))


if __name__ == "__main__":
    unittest.main()


class RequestedBatchTest(unittest.TestCase):
    def test_more_reply_and_all_sent_reply(self):
        self.assertTrue(app.requested_photos_reply("property_detail", more=True).startswith("Te envío más fotos"))
        self.assertIn("Ya te envié todas", app.all_photos_sent_reply("property_detail"))
        self.assertNotIn("visita", app.all_photos_sent_reply("handoff"))
