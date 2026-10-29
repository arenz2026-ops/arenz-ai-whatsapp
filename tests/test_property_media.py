import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app
import advisor_notify as an
import property_media as pm

BASE = {"property_id": "p", "public_reference": "ARZ-001", "operation": "compra", "property_type": "departamento",
        "district": "Surco", "zone": None, "public_location_reference": None, "price_amount": 200000, "currency": "USD",
        "bedrooms": 2, "bathrooms": 2, "area_m2": 80, "parking_spaces": 1, "features": [], "public_description": None,
        "lifecycle_state": "active_confirmed", "availability_confirmed_at": "2026-10-27T00:00:00+00:00",
        "approved_at": "2026-10-01T00:00:00+00:00"}


def media(reference, count, kind="photo", approved=True):
    return [{"media_type": kind, "media_url": f"https://cdn.example/{reference}/{n:02d}.jpg", "approved_for_client": approved}
            for n in range(1, count + 1)]


def match(reference, photos=0, district="Surco", **extra):
    row = {**BASE, "property_id": reference, "public_reference": reference, "district": district, **extra}
    return {"property": row, "public": app.public_property_snapshot(row, media(reference, photos))}


def said(content):
    """A session whose last assistant message is `content`."""
    return {"stage": "qualified", "state": {"recent_turns": [{"direction": "assistant", "content": content}]}}


def ok():
    response = Mock(); response.raise_for_status.return_value = None; return response


def urls(photos):
    return [photo["url"] for photo in photos]


class CaptionTests(unittest.TestCase):
    def test_only_approved_https_images_are_used(self):
        item = match("ARZ-001")
        item["public"]["media"] = [{"media_type": "link", "media_url": "https://x/tour"},
                                   {"media_type": "photo", "media_url": "http://x/insecure.jpg"},
                                   {"media_type": "photo", "media_url": "https://x/ok.jpg"},
                                   {"media_type": "photo", "media_url": "https://x/ok.jpg"}]
        self.assertEqual(pm.approved_images(item["public"]), ["https://x/ok.jpg"])

    def test_unapproved_photos_never_reach_the_snapshot(self):
        row = {**BASE}
        public = app.public_property_snapshot(row, media("ARZ-001", 2, approved=False) + media("ARZ-001", 1))
        self.assertEqual(pm.approved_images(public), ["https://cdn.example/ARZ-001/01.jpg"])

    def test_option_caption_matches_the_listing_and_hides_the_reference(self):
        item = match("C2-ABC123", 1, public_description="Proyecto Lavanda; unidad 402. Piso 4.")
        caption = pm.option_photos([item])[0]["caption"]
        self.assertEqual(caption, "Opción 1 · Lavanda · Unidad 402 · Surco · US$ 200.000 · 2 dorm.")
        self.assertIn("1. Lavanda · Unidad 402 · Surco", app.inventory_reply([item]))

    def test_captions_never_expose_the_internal_reference(self):
        item = match("ARZ-001", 2, public_description="Proyecto ARZ-001; unidad 5.")
        for photo in pm.unit_photos(item) + pm.option_photos([item]):
            self.assertNotIn("ARZ", photo["caption"])

    def test_unit_caption_counts_every_approved_photo(self):
        photos = pm.unit_photos(match("ARZ-001", 7))
        self.assertEqual(len(photos), 7)
        self.assertTrue(photos[0]["caption"].endswith("foto 1/7"))

    def test_listing_text_no_longer_pastes_photo_links(self):
        reply = app.inventory_reply([match("ARZ-001", 2)])
        self.assertNotIn("https://", reply); self.assertNotIn("Image:", reply); self.assertNotIn("Photo:", reply)


class WordingTests(unittest.TestCase):
    def test_photo_requests(self):
        for text in ("¿Me mandas fotos?", "tienes imágenes?", "quiero ver fotografías", "una foto porfa", "IMAGENES"):
            self.assertTrue(pm.is_photo_request(text), text)
        for text in ("no me mandes fotos", "No quiero ver más fotos", "¿Tiene cochera?", "fotocopia del DNI"):
            self.assertFalse(pm.is_photo_request(text), text)

    def test_option_numbers(self):
        cases = {"fotos de la 2": 2, "fotos de la opción 3": 3, "opcion 1 fotos": 1, "fotos del #2": 2,
                 "fotos de la segunda": 2, "de la primera opción": 1, "fotos": None, "primero mándame fotos": None,
                 "2 fotos": None}
        for text, expected in cases.items():
            self.assertEqual(pm.option_number(text), expected, text)

    def test_affirmatives(self):
        for text in ("sí", "Si", "Sí, por favor", "dale", "ok!", "claro que", "de acuerdo"):
            self.assertEqual(pm.is_affirmative(text), text != "claro que", text)
        self.assertFalse(pm.is_affirmative("no")); self.assertFalse(pm.is_affirmative("sí, pero de otra unidad"))


class PlanTests(unittest.TestCase):
    def test_options_get_their_main_photo_only(self):
        matches = [match("ARZ-001", 3), match("ARZ-002", 0), match("ARZ-003", 2, district="Lince")]
        photos, reply, stage, _ = app.apply_photo_plan(None, "busco en Surco", app.inventory_reply(matches), "qualified", "s", matches)
        self.assertEqual(urls(photos), ["https://cdn.example/ARZ-001/01.jpg", "https://cdn.example/ARZ-003/01.jpg"])
        self.assertTrue(photos[0]["caption"].startswith("Opción 1 · "))
        self.assertTrue(photos[1]["caption"].startswith("Opción 3 · Departamento · Lince"))
        self.assertEqual(stage, "qualified")

    def test_at_most_three_options_get_a_photo(self):
        matches = [match(f"ARZ-00{n}", 1) for n in range(1, 5)]
        listing = "\n".join(f"Ref. ARZ-00{n}" for n in range(1, 5))
        photos, *_ = app.apply_photo_plan(None, "ok", listing, "qualified", "s", matches)
        self.assertEqual(len(photos), pm.MAX_OPTIONS_WITH_PHOTO)

    def test_selecting_a_unit_sends_up_to_five_photos(self):
        matches = [match("ARZ-001", 1), match("ARZ-002", 7)]
        previous = said(app.inventory_conversation_reply(matches, {"purchase_timeline": "pronto", "payment_method": "contado"}))
        reply = app.selected_unit_reply(matches[1])
        photos, new_reply, stage, _ = app.apply_photo_plan(previous, "2", reply, "property_detail", "s", matches)
        self.assertEqual(len(photos), 5); self.assertTrue(all("ARZ-002" in url for url in urls(photos)))
        self.assertEqual((new_reply, stage), (reply, "property_detail"))

    def test_main_photo_already_sent_with_the_options_is_not_repeated(self):
        matches = [match("ARZ-001", 3)]
        previous = said(app.inventory_conversation_reply(matches, {"purchase_timeline": "pronto", "payment_method": "contado"}))
        sent = {"https://cdn.example/ARZ-001/01.jpg"}
        photos, *_ = app.apply_photo_plan(previous, "la 1", app.selected_unit_reply(matches[0]), "property_detail", "s", matches,
                                          lambda candidates: sent & set(candidates))
        self.assertEqual(urls(photos), ["https://cdn.example/ARZ-001/02.jpg", "https://cdn.example/ARZ-001/03.jpg"])

    def test_a_second_request_sends_the_next_photos(self):
        item = match("ARZ-001", 7)
        sent = {f"https://cdn.example/ARZ-001/{n:02d}.jpg" for n in range(1, 6)}
        photos, reply, _, _ = app.apply_photo_plan(said("Ref. ARZ-001\nUS$ 200.000."), "más fotos", "x", "conversation", "s", [item],
                                                   lambda candidates: sent & set(candidates))
        self.assertEqual([p["caption"][-8:] for p in photos], ["foto 6/7", "foto 7/7"])
        self.assertIn("Te comparto las fotos", reply)

    def test_a_parking_answer_does_not_resend_the_unit(self):
        item = match("ARZ-001", 3)
        photos, *_ = app.apply_photo_plan(said(app.selected_unit_reply(item)), "¿tiene cochera?",
                                          app.parking_question_reply(item), "property_detail", "s", [item])
        self.assertEqual(photos, [])

    def test_no_inventory_means_no_photos(self):
        self.assertEqual(app.apply_photo_plan(None, "fotos", "Ref. ARZ-001", "qualified", "s", None), ([], "Ref. ARZ-001", "qualified", "s"))


class PhotoRequestTests(unittest.TestCase):
    UNIT = "Revisemos esta unidad:\n\nRef. ARZ-001\nUS$ 200.000."

    def test_request_for_photos_of_the_unit_on_screen(self):
        item = match("ARZ-001", 2)
        photos, reply, stage, _ = app.apply_photo_plan(said(self.UNIT), "¿Me mandas fotos?", "respuesta IA", "conversation", "s", [item])
        self.assertEqual(len(photos), 2); self.assertEqual(stage, "property_detail")
        self.assertTrue(reply.startswith("Ref. ARZ-001"))
        self.assertEqual(app.customer_visible_reply(reply), "Te comparto las fotos de esta unidad. ¿Qué más te gustaría saber de ella?")

    def test_request_names_an_option_by_number(self):
        matches = [match("ARZ-001", 2), match("ARZ-002", 3)]
        previous = said(app.inventory_conversation_reply(matches, {"purchase_timeline": "pronto", "payment_method": "contado"}))
        photos, reply, stage, _ = app.apply_photo_plan(previous, "¿tienes fotos de la segunda?", "x", "conversation", "s", matches)
        self.assertEqual(len(photos), 3); self.assertTrue(all("ARZ-002" in url for url in urls(photos)))
        self.assertTrue(reply.startswith("Ref. ARZ-002")); self.assertEqual(stage, "property_detail")

    def test_option_number_after_viewing_one_unit_points_at_the_listing(self):
        matches = [match("ARZ-001", 2), match("ARZ-002", 3)]
        listing = app.inventory_conversation_reply(matches, {"purchase_timeline": "pronto", "payment_method": "contado"})
        unit = app.selected_unit_reply(matches[0])
        recent = {"stage": "property_detail", "state": {"recent_turns": [
            {"direction": "assistant", "content": listing}, {"direction": "user", "content": "1"},
            {"direction": "assistant", "content": unit}]}}
        forgotten = said(unit)  # the listing already left the turn window
        for previous in (recent, forgotten):
            photos, reply, _, _ = app.apply_photo_plan(previous, "fotos de la 2", unit, "property_detail", "s", matches)
            self.assertTrue(reply.startswith("Ref. ARZ-002")); self.assertEqual(len(photos), 3)

    def test_ambiguous_request_asks_which_option_and_the_answer_selects_it(self):
        matches = [match("ARZ-001", 2), match("ARZ-002", 3), match("ARZ-003", 1)]
        previous = said(app.inventory_conversation_reply(matches, {"purchase_timeline": "pronto", "payment_method": "contado"}))
        photos, reply, stage, _ = app.apply_photo_plan(previous, "mándame fotos", app.inventory_reply(matches), "qualified", "s", matches)
        self.assertEqual(photos, [])
        self.assertEqual(app.customer_visible_reply(reply), "¿De cuál opción quieres ver las fotos: 1, 2 o 3?")
        self.assertEqual(app.selected_option(said(reply), "2")["reference"], "ARZ-002")

    def test_request_without_photos_is_answered_honestly_with_an_advisor_offer(self):
        item = match("ARZ-001", 0)
        photos, reply, _, _ = app.apply_photo_plan(said(self.UNIT), "tienes imágenes?", "respuesta IA", "conversation", "s", [item])
        visible = app.customer_visible_reply(reply)
        self.assertEqual(photos, [])
        self.assertIn("no tengo fotos de esta unidad", visible); self.assertIn("asesor", visible)
        self.assertNotIn("ARZ", visible)

    def test_request_after_every_photo_was_sent_does_not_promise_photos(self):
        item = match("ARZ-001", 2)
        photos, reply, _, _ = app.apply_photo_plan(said(self.UNIT), "más fotos", "x", "conversation", "s", [item],
                                                   lambda candidates: set(candidates))
        self.assertEqual(photos, [])
        self.assertIn("Ya te envié todas las fotos", reply); self.assertNotIn("Te comparto", reply)

    def test_parking_and_photos_in_one_message_answers_both(self):
        item = match("ARZ-001", 0)
        previous = {**said(self.UNIT), "stage": "property_detail"}
        parking = app.parking_question_reply(item)
        photos, reply, stage, _ = app.apply_photo_plan(previous, "¿tiene cochera? y mándame fotos", parking, "property_detail", "s", [item])
        self.assertTrue(reply.startswith(parking)); self.assertIn("no tengo fotos de esta unidad", reply)
        self.assertEqual((photos, stage), ([], "property_detail"))

    def test_request_with_no_unit_in_view_changes_nothing(self):
        result = app.apply_photo_plan(None, "quiero ver fotos de departamentos", "respuesta IA", "qualification", "s", [match("ARZ-001", 2)])
        self.assertEqual(result, ([], "respuesta IA", "qualification", "s"))

    def test_new_listing_with_a_photo_request_shows_the_new_options(self):
        old = [match("ARZ-001", 2), match("ARZ-002", 2)]
        new = [match("ARZ-005", 2), match("ARZ-006", 2)]
        previous = said(app.inventory_reply(old))
        listing = app.inventory_reply(new)
        photos, reply, stage, _ = app.apply_photo_plan(previous, "y en Lince, con fotos", listing, "qualified", "s", new)
        self.assertEqual((reply, stage), (listing, "qualified"))
        self.assertEqual(urls(photos), ["https://cdn.example/ARZ-005/01.jpg", "https://cdn.example/ARZ-006/01.jpg"])

    def test_handoff_reply_is_never_replaced(self):
        for stage in ("handoff", "do_not_contact", "awaiting_customer"):
            result = app.apply_photo_plan(said(self.UNIT), "no me interesa, ni fotos", "Respuesta", stage, "s", [match("ARZ-001", 2)])
            self.assertEqual(result, ([], "Respuesta", stage, "s"))

    def test_refusal_is_not_a_request(self):
        result = app.apply_photo_plan(said(self.UNIT), "no me mandes fotos", "Respuesta", "conversation", "s", [match("ARZ-001", 2)])
        self.assertEqual(result, ([], "Respuesta", "conversation", "s"))


class AdvisorOfferTests(unittest.TestCase):
    def offered(self):
        item = match("ARZ-001", 0)
        _, reply, _, _ = app.apply_photo_plan(said("Ref. ARZ-001\nUS$ 200.000."), "fotos?", "x", "conversation", "s", [item])
        return said(reply)

    def test_yes_to_the_offer_hands_off_with_the_unit(self):
        observation = {"intent": "confirmation", "slot_updates": {}, "handoff": False}
        reply, _, stage, summary = app.progressive_reply(self.offered(), observation, "fallback", "Sí, por favor", {"operation": "compra"})
        self.assertEqual(stage, "handoff")
        self.assertIn("te envíe las fotos", reply); self.assertNotIn("ARZ", app.customer_visible_reply(reply))
        memory = Mock(); memory.commit_work.return_value = []
        app.commit_consistent_turn(memory, "c", "51911111111", "m", "Sí, por favor", reply, observation, {"operation": "compra"},
                                   stage, summary, self.offered(), {"active_search_id": "s1", "searches": {"s1": {}}})
        handoff = memory.commit_work.call_args.args[0]["p_handoff"]
        self.assertEqual((handoff["request_type"], handoff["property_reference"]), ("property_interest", "ARZ-001"))

    def test_advisor_notice_says_photos_were_asked(self):
        handoff = app.detected_handoff(self.offered(), None, "sí")
        self.assertTrue(an.advisor_text("519", handoff, {}, "sí").startswith("🔔 Nuevo lead para atender — Pidió fotos de un inmueble"))
        self.assertIn("Inmueble: ARZ-001", an.advisor_text("519", handoff, {}, "sí"))

    def test_yes_without_an_offer_is_not_a_handoff(self):
        self.assertIsNone(app.detected_handoff(said("Ref. ARZ-001\nUS$ 200.000."), None, "sí"))


class CustomerCopyTests(unittest.TestCase):
    def test_arz_references_are_hidden_like_c2(self):
        self.assertEqual(app.customer_visible_reply("Registré tu interés en la referencia ARZ-000001."),
                         "Registré tu interés en la unidad elegida.")

    def test_image_payload(self):
        with patch.object(app.requests, "post", return_value=ok()) as post:
            app.send_whatsapp_image("519", "https://x/1.jpg", "Opción 1 · Surco · ARZ-001")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["type"], "image")
        self.assertEqual(body["image"], {"link": "https://x/1.jpg", "caption": "Opción 1 · Surco · la unidad elegida"})

    def test_image_without_caption(self):
        with patch.object(app.requests, "post", return_value=ok()) as post:
            app.send_whatsapp_image("519", "https://x/1.jpg", "")
        self.assertEqual(post.call_args.kwargs["json"]["image"], {"link": "https://x/1.jpg"})


class DeliveryLogTests(unittest.TestCase):
    PHOTOS = [{"reference": "ARZ-001", "url": "https://x/1.jpg", "caption": "a"},
              {"reference": "ARZ-001", "url": "https://x/2.jpg", "caption": "b"}]

    def deliver(self, log, search, photos, send=None):
        send = send or Mock()
        pending = pm.unsent(photos, lambda candidates: log.already_sent("519", search, candidates))
        return pm.send_photos(send, log, "519", search, pending), send

    def test_photos_are_not_repeated_in_the_same_search(self):
        log = pm.MediaDeliveryLog()
        self.assertEqual(self.deliver(log, "s1", self.PHOTOS)[0], 2)
        self.assertEqual(self.deliver(log, "s1", self.PHOTOS)[0], 0)
        self.assertEqual(self.deliver(log, "s2", self.PHOTOS[:1])[0], 1)

    def test_failed_photo_is_offered_again_later(self):
        log = pm.MediaDeliveryLog()
        self.deliver(log, "s1", self.PHOTOS, Mock(side_effect=[RuntimeError("x"), None]))
        _, send = self.deliver(log, "s1", self.PHOTOS)
        self.assertEqual([c.args[1] for c in send.call_args_list], ["https://x/1.jpg"])

    def test_supabase_log_reads_and_records(self):
        log = pm.MediaDeliveryLog("https://db.example", "k")
        got = ok(); got.json.return_value = [{"media_url": "https://x/1.jpg"}]
        with patch.object(pm.requests, "get", return_value=got) as get, patch.object(pm.requests, "post", return_value=ok()) as post:
            _, send = self.deliver(log, "s1", self.PHOTOS)
        self.assertEqual([c.args[1] for c in send.call_args_list], ["https://x/2.jpg"])
        self.assertEqual(get.call_args.kwargs["params"]["search_key"], "eq.s1")
        self.assertEqual(post.call_args.kwargs["json"], {"phone": "519", "search_key": "s1", "media_url": "https://x/2.jpg"})
        self.assertIn("on_conflict=phone,search_key,media_url", post.call_args.args[0])

    def test_supabase_outage_falls_back_to_memory(self):
        log = pm.MediaDeliveryLog("https://db.example", "k")
        with patch.object(pm.requests, "get", side_effect=pm.requests.ConnectionError()), \
             patch.object(pm.requests, "post", side_effect=pm.requests.ConnectionError()):
            self.assertEqual(self.deliver(log, "s1", self.PHOTOS)[0], 2)
            self.assertEqual(self.deliver(log, "s1", self.PHOTOS)[0], 0)

    def test_broken_check_never_blocks_photos(self):
        self.assertEqual(pm.unsent(self.PHOTOS, Mock(side_effect=RuntimeError("x"))), self.PHOTOS)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db")
        os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        app.APP_SECRET = "test-app-secret"; app.OPENAI_API_KEY = ""; app.user_sessions.clear(); app._media_log = None
        self.client = app.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def post(self, side_effect, message_id="wamid-photo"):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": message_id, "from": "51911111111", "type": "text", "text": {"body": "hola"}}]}}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(app.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()}
        planned = [{"reference": "ARZ-001", "url": "https://x/1.jpg", "caption": "Opción 1"}]

        def plan(previous, text, reply, stage, summary, matches, sent_before):
            return pm.unsent(planned, sent_before), reply, stage, summary

        with patch.object(app, "apply_photo_plan", side_effect=plan), \
             patch.object(app.requests, "post", side_effect=side_effect) as post:
            result = self.client.post("/webhook", data=raw, headers=headers)
        return result, post

    def test_photos_go_before_the_text_reply(self):
        result, post = self.post([ok(), ok()])
        self.assertEqual(result.status_code, 200)
        self.assertEqual([c.kwargs["json"]["type"] for c in post.call_args_list], ["image", "text"])

    def test_same_photo_is_not_sent_twice_to_the_same_client(self):
        self.post([ok(), ok()], "wamid-1")
        _, post = self.post([ok(), ok()], "wamid-2")
        self.assertEqual([c.kwargs["json"]["type"] for c in post.call_args_list], ["text"])

    def test_failed_photo_still_sends_the_text(self):
        failing = ok(); failing.raise_for_status.side_effect = app.requests.HTTPError("bad link"); failing.json.return_value = {}
        result, post = self.post([failing, ok()])
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual(post.call_args_list[-1].kwargs["json"]["type"], "text")


if __name__ == "__main__":
    unittest.main()
