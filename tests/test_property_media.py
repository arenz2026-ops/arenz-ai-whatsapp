import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app
import property_media as pm

BASE = {"property_id": "p", "public_reference": "ARZ-001", "operation": "compra", "property_type": "departamento",
        "district": "Surco", "zone": None, "public_location_reference": None, "price_amount": 200000, "currency": "USD",
        "bedrooms": 2, "bathrooms": 2, "area_m2": 80, "parking_spaces": 1, "features": [], "public_description": None,
        "lifecycle_state": "active_confirmed", "availability_confirmed_at": "2026-10-27T00:00:00+00:00",
        "approved_at": "2026-10-01T00:00:00+00:00"}


def media(reference, count, kind="image"):
    return [{"media_type": kind, "media_url": f"https://cdn.example/{reference}/{n:02d}.jpg", "approved_for_client": True}
            for n in range(1, count + 1)]


def match(reference, photos=0, district="Surco", **extra):
    row = {**BASE, "property_id": reference, "public_reference": reference, "district": district, **extra}
    return {"property": row, "public": app.public_property_snapshot(row, media(reference, photos))}


def ok():
    response = Mock(); response.raise_for_status.return_value = None; return response


class PlanTests(unittest.TestCase):
    def test_options_get_their_main_photo_only(self):
        matches = [match("ARZ-001", 3), match("ARZ-002", 0), match("ARZ-003", 2, district="Lince")]
        photos, requested = pm.plan_photos(app.inventory_reply(matches), "qualified", matches)
        self.assertIsNone(requested)
        self.assertEqual([p["url"] for p in photos], ["https://cdn.example/ARZ-001/01.jpg", "https://cdn.example/ARZ-003/01.jpg"])
        self.assertEqual(photos[0]["caption"], "Opción 1 · Departamento · Surco · US$ 200.000 · 2 dorm.")
        self.assertTrue(photos[1]["caption"].startswith("Opción 3 · Departamento · Lince"))

    def test_selected_unit_sends_up_to_five_photos(self):
        item = match("ARZ-001", 7)
        photos, _ = pm.plan_photos(app.selected_unit_reply(item), "property_detail", [item])
        self.assertEqual(len(photos), pm.MAX_UNIT_PHOTOS)
        self.assertTrue(photos[0]["caption"].endswith("foto 1/5"))

    def test_only_approved_https_images_are_used(self):
        item = match("ARZ-001")
        item["public"]["media"] = [{"media_type": "video", "media_url": "https://x/v.mp4"},
                                   {"media_type": "image", "media_url": "http://x/insecure.jpg"},
                                   {"media_type": "Foto", "media_url": "https://x/ok.jpg"}]
        self.assertEqual(pm.approved_images(item["public"]), ["https://x/ok.jpg"])

    def test_captions_never_expose_the_internal_reference(self):
        item = match("ARZ-001", 2)
        photos, _ = pm.plan_photos(app.selected_unit_reply(item), "property_detail", [item])
        self.assertTrue(all("ARZ" not in p["caption"] for p in photos))

    def test_no_inventory_means_no_photos(self):
        self.assertEqual(pm.plan_photos("Ref. ARZ-001", "qualified", None), ([], None))

    def test_listing_text_no_longer_pastes_photo_links(self):
        self.assertNotIn("https://cdn.example", app.inventory_reply([match("ARZ-001", 2)]))


class PhotoRequestTests(unittest.TestCase):
    PREVIOUS = {"stage": "property_detail", "state": {"recent_turns": [
        {"direction": "assistant", "content": "Revisemos esta unidad:\n\nRef. ARZ-001\nUS$ 200.000."}]}}

    def test_request_for_photos_of_the_unit_on_screen(self):
        item = match("ARZ-001", 2)
        photos, reply, stage, _ = app.apply_photo_plan(self.PREVIOUS, "¿Me mandas fotos?", "respuesta IA", "conversation", "s", [item])
        self.assertEqual(len(photos), 2); self.assertEqual(stage, "property_detail")
        self.assertIn("Te comparto las fotos", reply); self.assertTrue(reply.startswith("Ref. ARZ-001"))

    def test_request_without_photos_is_answered_honestly(self):
        item = match("ARZ-001", 0)
        photos, reply, _, _ = app.apply_photo_plan(self.PREVIOUS, "tienes imágenes?", "respuesta IA", "conversation", "s", [item])
        self.assertEqual(photos, []); self.assertIn("no tengo fotos aprobadas", reply)

    def test_request_with_no_unit_in_view_changes_nothing(self):
        photos, reply, stage, _ = app.apply_photo_plan(None, "quiero ver fotos de departamentos", "respuesta IA", "qualification", "s", [match("ARZ-001", 2)])
        self.assertEqual((photos, reply, stage), ([], "respuesta IA", "qualification"))

    def test_handoff_reply_is_never_replaced(self):
        photos, reply, stage, _ = app.apply_photo_plan(self.PREVIOUS, "me interesa, mándame fotos", "Registré tu interés", "handoff", "s", [match("ARZ-001", 2)])
        self.assertEqual((reply, stage), ("Registré tu interés", "handoff"))


class SendTests(unittest.TestCase):
    PHOTOS = [{"reference": "ARZ-001", "url": "https://x/1.jpg", "caption": "a"},
              {"reference": "ARZ-001", "url": "https://x/2.jpg", "caption": "b"}]

    def test_photos_are_not_repeated_in_the_same_search(self):
        log, send = pm.MediaDeliveryLog(), Mock()
        self.assertEqual(pm.send_photos(send, log, "519", "s1", self.PHOTOS), 2)
        self.assertEqual(pm.send_photos(send, log, "519", "s1", self.PHOTOS), 0)
        self.assertEqual(pm.send_photos(send, log, "519", "s2", self.PHOTOS[:1]), 1)
        self.assertEqual(send.call_count, 3)

    def test_failed_photo_is_retried_on_a_later_turn(self):
        log = pm.MediaDeliveryLog()
        pm.send_photos(Mock(side_effect=[RuntimeError("x"), None]), log, "519", "s1", self.PHOTOS)
        send = Mock(); pm.send_photos(send, log, "519", "s1", self.PHOTOS)
        self.assertEqual([c.args[1] for c in send.call_args_list], ["https://x/1.jpg"])

    def test_supabase_log_reads_and_records(self):
        log = pm.MediaDeliveryLog("https://db.example", "k")
        got = ok(); got.json.return_value = [{"media_url": "https://x/1.jpg"}]
        with patch.object(pm.requests, "get", return_value=got), patch.object(pm.requests, "post", return_value=ok()) as post:
            send = Mock(); pm.send_photos(send, log, "519", "s1", self.PHOTOS)
        self.assertEqual([c.args[1] for c in send.call_args_list], ["https://x/2.jpg"])
        self.assertEqual(post.call_args.kwargs["json"], {"phone": "519", "search_key": "s1", "media_url": "https://x/2.jpg"})

    def test_missing_log_table_does_not_stop_photos(self):
        log = pm.MediaDeliveryLog("https://db.example", "k")
        with patch.object(pm.requests, "get", side_effect=pm.requests.ConnectionError()), \
             patch.object(pm.requests, "post", side_effect=pm.requests.ConnectionError()):
            send = Mock(); self.assertEqual(pm.send_photos(send, log, "519", "s1", self.PHOTOS), 2)

    def test_image_payload(self):
        with patch.object(app.requests, "post", return_value=ok()) as post:
            app.send_whatsapp_image("519", "https://x/1.jpg", "Opción 1 · Surco")
        self.assertEqual(post.call_args.kwargs["json"]["image"], {"link": "https://x/1.jpg", "caption": "Opción 1 · Surco"})
        self.assertEqual(post.call_args.kwargs["json"]["type"], "image")


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db")
        os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        app.APP_SECRET = "test-app-secret"; app.OPENAI_API_KEY = ""; app.user_sessions.clear(); app._media_log = None
        self.client = app.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def post(self, side_effect):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-photo", "from": "51911111111", "type": "text", "text": {"body": "hola"}}]}}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(app.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()}
        planned = [{"reference": "ARZ-001", "url": "https://x/1.jpg", "caption": "Opción 1"}]
        with patch.object(app, "apply_photo_plan", side_effect=lambda p, t, r, st, su, m: (planned, r, st, su)), \
             patch.object(app.requests, "post", side_effect=side_effect) as post:
            result = self.client.post("/webhook", data=raw, headers=headers)
        return result, post

    def test_photos_go_before_the_text_reply(self):
        result, post = self.post([ok(), ok()])
        self.assertEqual(result.status_code, 200)
        self.assertEqual([c.kwargs["json"]["type"] for c in post.call_args_list], ["image", "text"])

    def test_failed_photo_still_sends_the_text(self):
        failing = ok(); failing.raise_for_status.side_effect = app.requests.HTTPError("bad link"); failing.json.return_value = {}
        result, post = self.post([failing, ok()])
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual(post.call_args_list[-1].kwargs["json"]["type"], "text")


if __name__ == "__main__":
    unittest.main()
