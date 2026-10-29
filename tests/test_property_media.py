import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import advisor_notify
import app
import property_media as pm

BASE = {"property_id": "p", "public_reference": "ARZ-001", "operation": "compra", "property_type": "departamento",
        "district": "Surco", "zone": None, "public_location_reference": None, "price_amount": 200000, "currency": "USD",
        "bedrooms": 2, "bathrooms": 2, "area_m2": 80, "parking_spaces": 1, "features": [], "public_description": None,
        "lifecycle_state": "active_confirmed", "availability_confirmed_at": "2026-10-27T00:00:00+00:00",
        "approved_at": "2026-10-01T00:00:00+00:00"}
CONFIRM = {"intent": "confirmation", "slot_updates": {}, "criteria_actions": [], "criteria_change": False, "handoff": False}


def media(reference, count, kind="photo"):
    """'photo' is the only image type inventory_property_media accepts."""
    return [{"media_type": kind, "media_url": f"https://cdn.example/{reference}/{n:02d}.jpg", "approved_for_client": True}
            for n in range(1, count + 1)]


def match(reference, photos=0, district="Surco", **extra):
    row = {**BASE, "property_id": reference, "public_reference": reference, "district": district, **extra}
    return {"property": row, "public": app.public_property_snapshot(row, media(reference, photos))}


def said(*replies, stage="qualified"):
    """A session whose recent turns are these assistant replies, as stored (with Ref. lines)."""
    return {"stage": stage, "state": {"recent_turns": [{"direction": "assistant", "content": reply} for reply in replies]}}


def listing(matches):
    return said(app.inventory_reply(matches) + "\n\n¿Cuál de estas unidades te gustaría revisar con más detalle?")


def urls(photos):
    return [photo["url"] for photo in photos]


def ok():
    response = Mock(); response.raise_for_status.return_value = None; return response


class Isolated(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False); self.env.start()
        os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        app._media_log = None

    def tearDown(self):
        self.env.stop(); app._media_log = None


class PlanTests(Isolated):
    def test_options_get_their_main_photo_only(self):
        matches = [match("ARZ-001", 3), match("ARZ-002", 0), match("ARZ-003", 2, district="Lince")]
        photos, unit = pm.plan_photos(app.inventory_reply(matches), "qualified", matches)
        self.assertIsNone(unit)
        self.assertEqual(urls(photos), ["https://cdn.example/ARZ-001/01.jpg", "https://cdn.example/ARZ-003/01.jpg"])
        self.assertEqual(photos[0]["caption"], "Opción 1 · Departamento · Surco · US$ 200.000 · 2 dorm.")
        self.assertTrue(photos[1]["caption"].startswith("Opción 3 · Departamento · Lince"))

    def test_units_of_one_project_do_not_repeat_its_photo(self):
        first, second = match("C2-A", 2), match("C2-B", 2)
        second["public"]["media"] = first["public"]["media"]  # project photos shared by both units
        photos, _ = pm.plan_photos(app.inventory_reply([first, second]), "qualified", [first, second])
        self.assertEqual(urls(photos), ["https://cdn.example/C2-A/01.jpg"])

    def test_only_a_listing_gets_option_photos(self):
        item = match("ARZ-001", 3)
        self.assertEqual(pm.plan_photos(app.parking_question_reply(item), "property_detail", [item]), ([], None))

    def test_only_approved_https_images_are_used(self):
        item = match("ARZ-001")
        item["public"]["media"] = [{"media_type": "link", "media_url": "https://x/tour"},
                                   {"media_type": "photo", "media_url": "http://x/insecure.jpg"},
                                   {"media_type": "photo", "media_url": "https://x/ok.jpg"}]
        self.assertEqual(pm.approved_images(item["public"]), ["https://x/ok.jpg"])

    def test_no_inventory_means_no_photos(self):
        self.assertEqual(pm.plan_photos("Ref. ARZ-001", "qualified", None), ([], None))
        self.assertEqual(app.apply_photo_plan(None, "fotos", "r", "qualified", "s", None), ([], "r", "qualified", "s"))

    def test_listing_text_no_longer_pastes_photo_links(self):
        self.assertNotIn("https://cdn.example", app.inventory_reply([match("ARZ-001", 2)]))


class SelectionTests(Isolated):
    def choose(self, previous, text, matches, phone=None):
        reply, _, stage, summary = app.progressive_reply(previous, CONFIRM, "fallback", text, inventory_matches=matches)
        return app.apply_photo_plan(previous, text, reply, stage, summary, matches, phone, "s1")

    def test_choosing_an_option_sends_up_to_five_photos_of_it(self):
        matches = [match("ARZ-001", 7), match("ARZ-002", 2)]
        photos, reply, stage, _ = self.choose(listing(matches), "1", matches)
        self.assertEqual(stage, "property_detail")
        self.assertTrue(reply.startswith("Revisemos esta unidad"))
        self.assertEqual(urls(photos), [f"https://cdn.example/ARZ-001/0{n}.jpg" for n in range(1, 6)])
        self.assertEqual(photos[0]["caption"], "Departamento · Surco · US$ 200.000 · 2 dorm. · foto 1/7")
        self.assertTrue(all("ARZ" not in photo["caption"] for photo in photos))

    def test_asking_again_brings_the_photos_not_sent_yet(self):
        item = match("ARZ-001", 7)
        photos, _, _, _ = self.choose(listing([item, match("ARZ-002")]), "1", [item, match("ARZ-002")], phone="519")
        for photo in photos:
            app.get_media_log().record("519", "s1", photo["url"])
        unit = said(app.selected_unit_reply(item), stage="property_detail")
        photos, reply, _, _ = app.apply_photo_plan(unit, "¿tienes más fotos?", "r", "property_detail", "s", [item], "519", "s1")
        self.assertEqual(urls(photos), ["https://cdn.example/ARZ-001/06.jpg", "https://cdn.example/ARZ-001/07.jpg"])
        self.assertIn("Te comparto más fotos", reply)
        for photo in photos:
            app.get_media_log().record("519", "s1", photo["url"])
        photos, reply, _, _ = app.apply_photo_plan(unit, "más fotos", "r", "property_detail", "s", [item], "519", "s1")
        self.assertEqual(photos, [])
        self.assertIn("Ya te envié todas las fotos", reply)
        self.assertIn("pedirle a un asesor", reply)

    def test_a_parking_question_sends_no_photos(self):
        item = match("ARZ-001", 3)
        unit = said(app.selected_unit_reply(item), stage="property_detail")
        photos, reply, _, _ = app.apply_photo_plan(unit, "¿la cochera está incluida?", app.parking_question_reply(item),
                                                   "property_detail", "s", [item])
        self.assertEqual(photos, [])
        self.assertIn("estacionamiento", reply)


class PhotoRequestTests(Isolated):
    PREVIOUS = said("Revisemos esta unidad:\n\nRef. ARZ-001\nUS$ 200.000.", stage="property_detail")

    def test_request_for_photos_of_the_unit_on_screen(self):
        item = match("ARZ-001", 2)
        photos, reply, stage, _ = app.apply_photo_plan(self.PREVIOUS, "¿Me mandas fotos?", "respuesta IA", "conversation", "s", [item])
        self.assertEqual(len(photos), 2); self.assertEqual(stage, "property_detail")
        self.assertIn("Te comparto las fotos", reply); self.assertTrue(reply.startswith("Ref. ARZ-001"))
        self.assertNotIn("ARZ", app.customer_visible_reply(reply))

    def test_request_without_photos_is_answered_honestly(self):
        item = match("ARZ-001", 0)
        photos, reply, _, _ = app.apply_photo_plan(self.PREVIOUS, "tienes imágenes?", "respuesta IA", "conversation", "s", [item])
        self.assertEqual(photos, [])
        self.assertIn("no tengo fotos de esta unidad", reply)
        self.assertIn("Puedo pedirle a un asesor que te las envíe", reply)

    def test_request_by_option_number(self):
        matches = [match("ARZ-001", 1), match("ARZ-002", 2), match("ARZ-003", 3)]
        for text, reference in (("mándame fotos de la 2", "ARZ-002"), ("fotos de la segunda", "ARZ-002"),
                                ("¿tienes fotos de la opción 3?", "ARZ-003")):
            photos, _, stage, _ = app.apply_photo_plan(listing(matches), text, "r", "qualified", "s", matches)
            self.assertEqual({photo["reference"] for photo in photos}, {reference}, text)
            self.assertEqual(stage, "property_detail")

    def test_request_with_several_options_asks_which_one(self):
        matches = [match("ARZ-001", 1), match("ARZ-002", 2)]
        for text in ("¿tienes fotos?", "fotos de la 5"):
            photos, reply, _, _ = app.apply_photo_plan(listing(matches), text, "r", "qualified", "s", matches)
            self.assertEqual(photos, [])
            self.assertEqual(app.customer_visible_reply(reply), "¿De cuál de estas opciones quieres ver las fotos? Respóndeme con su número.")
        self.assertEqual(app.selected_option(said(reply), "2")["reference"], "ARZ-002")

    def test_refusing_photos_is_not_a_request(self):
        for text in ("no me mandes fotos", "no quiero fotos, gracias", "no necesito imágenes"):
            self.assertFalse(pm.is_photo_request(text), text)
        for text in ("¿no tienes fotos?", "Fotos por favor", "tienes fotografías de la sala?"):
            self.assertTrue(pm.is_photo_request(text), text)

    def test_request_with_no_unit_in_view_changes_nothing(self):
        photos, reply, stage, _ = app.apply_photo_plan(None, "quiero ver fotos de departamentos", "respuesta IA", "qualification", "s", [match("ARZ-001", 2)])
        self.assertEqual((photos, reply, stage), ([], "respuesta IA", "qualification"))

    def test_handoff_and_pause_replies_are_never_replaced(self):
        for stage, reply in (("handoff", "Registré tu interés"), ("do_not_contact", "Entendido."), ("awaiting_customer", "Claro.")):
            photos, got, got_stage, _ = app.apply_photo_plan(self.PREVIOUS, "no me interesa, ni fotos", reply, stage, "s", [match("ARZ-001", 2)])
            self.assertEqual((photos, got, got_stage), ([], reply, stage))


class AdvisorOfferTests(Isolated):
    def offered(self):
        return said(pm.photo_request_reply(match("ARZ-001"), 0, 0), stage="property_detail")

    def test_yes_to_the_offer_hands_off_with_the_unit(self):
        item = match("ARZ-001")
        reply, _, stage, summary = app.progressive_reply(self.offered(), CONFIRM, "fallback", "Sí, por favor", inventory_matches=[item])
        self.assertEqual(stage, "handoff")
        self.assertIn("te enviará las fotos", reply)
        self.assertIn("ARZ-001", summary)
        handoff = app.detected_handoff(self.offered(), CONFIRM, "Sí, por favor", [item])
        self.assertEqual(handoff, {"request_type": "property_interest", "property_reference": "ARZ-001", "reason": "photos"})
        payload = app._handoff_payload("519", handoff, "s1", {})
        self.assertEqual((payload["request_type"], payload["property_reference"]), ("property_interest", "ARZ-001"))
        photos, _, _, _ = app.apply_photo_plan(self.offered(), "Sí, por favor", reply, stage, summary, [item])
        self.assertEqual(photos, [])

    def test_only_a_plain_yes_counts(self):
        for text in ("sí", "Si", "dale", "ok, gracias", "si por favor", "De acuerdo", "Sí, que me las envíe", "ya"):
            self.assertTrue(pm.is_affirmative(text), text)
        for text in ("no", "no, gracias", "si tienes otra opción avísame", "ya vi las fotos", "bueno pero prefiero otra", ""):
            self.assertFalse(pm.is_affirmative(text), text)
        self.assertIsNone(app.detected_handoff(self.offered(), CONFIRM, "si tienes otra opción avísame", [match("ARZ-001")]))

    def test_yes_without_an_offer_is_not_a_handoff(self):
        previous = said(app.selected_unit_reply(match("ARZ-001")), stage="property_detail")
        self.assertIsNone(app.detected_handoff(previous, CONFIRM, "sí", [match("ARZ-001")]))

    def test_the_advisor_is_told_the_client_wants_photos(self):
        handoff = {"request_type": "property_interest", "property_reference": "ARZ-001", "reason": "photos"}
        text = advisor_notify.advisor_text("519", handoff, {}, "sí")
        self.assertIn("Pidió fotos de un inmueble", text); self.assertIn("Inmueble: ARZ-001", text)
        params = advisor_notify.template_payload("51900", "aviso", "es", "519", handoff, {})["template"]["components"][0]["parameters"]
        self.assertEqual(params[1]["text"], "Pidió fotos de un inmueble")


class CustomerCopyTests(unittest.TestCase):
    def test_handoff_copy_hides_the_arz_reference(self):
        for reply in (app.handoff_reply({"request_type": "property_interest", "property_reference": "ARZ-000001"})[0],
                      app.handoff_reply({"request_type": "visit", "property_reference": "ARZ-000001"})[0]):
            visible = app.customer_visible_reply(reply)
            self.assertNotIn("ARZ", visible); self.assertIn("la unidad elegida", visible)

    def test_captions_never_expose_the_internal_reference(self):
        with patch.object(app.requests, "post", return_value=ok()) as post:
            app.send_whatsapp_image("519", "https://x/1.jpg", "Opción 1 · ARZ-000001 · Surco")
        self.assertNotIn("ARZ", post.call_args.kwargs["json"]["image"]["caption"])

    def test_image_link_must_be_https(self):
        with patch.object(app.requests, "post") as post, self.assertRaises(ValueError):
            app.send_whatsapp_image("519", "http://x/1.jpg", "c")
        post.assert_not_called()

    def test_image_payload(self):
        with patch.object(app.requests, "post", return_value=ok()) as post:
            app.send_whatsapp_image("519", "https://x/1.jpg", "Opción 1 · Surco")
        self.assertEqual(post.call_args.kwargs["json"]["image"], {"link": "https://x/1.jpg", "caption": "Opción 1 · Surco"})
        self.assertEqual(post.call_args.kwargs["json"]["type"], "image")


class SendTests(Isolated):
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
        self.assertIn("on_conflict=phone,search_key,media_url", post.call_args.args[0])

    def test_missing_log_table_does_not_stop_photos(self):
        log = pm.MediaDeliveryLog("https://db.example", "k")
        with patch.object(pm.requests, "get", side_effect=pm.requests.ConnectionError()), \
             patch.object(pm.requests, "post", side_effect=pm.requests.ConnectionError()):
            send = Mock(); self.assertEqual(pm.send_photos(send, log, "519", "s1", self.PHOTOS), 2)

    def test_in_memory_log_survives_across_turns_without_supabase_key(self):
        os.environ["SUPABASE_URL"] = "https://db.example"
        self.assertIs(app.get_media_log(), app.get_media_log())
        self.assertEqual(app.get_media_log().url, "")


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
        with patch.object(app, "apply_photo_plan", side_effect=lambda p, t, r, st, su, m, *rest: (planned, r, st, su)), \
             patch.object(app.requests, "post", side_effect=side_effect) as post:
            result = self.client.post("/webhook", data=raw, headers=headers)
        return result, post

    def test_photos_go_before_the_text_reply(self):
        result, post = self.post([ok(), ok()])
        self.assertEqual(result.status_code, 200)
        self.assertEqual([c.kwargs["json"]["type"] for c in post.call_args_list], ["image", "text"])

    def test_a_photo_planning_error_still_sends_the_text(self):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid-plan", "from": "51911111111", "type": "text", "text": {"body": "hola"}}]}}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(app.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()}
        with patch.object(app, "apply_photo_plan", side_effect=ValueError("bad row")), \
             patch.object(app.requests, "post", return_value=ok()) as post:
            result = self.client.post("/webhook", data=raw, headers=headers)
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual([c.kwargs["json"]["type"] for c in post.call_args_list], ["text"])

    def test_failed_photo_still_sends_the_text(self):
        failing = ok(); failing.raise_for_status.side_effect = app.requests.HTTPError("bad link"); failing.json.return_value = {}
        result, post = self.post([failing, ok()])
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual(post.call_args_list[-1].kwargs["json"]["type"], "text")


if __name__ == "__main__":
    unittest.main()
