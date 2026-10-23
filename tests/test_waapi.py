import os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app

SECRET = "s3cret-path-token-for-tests"
INSTANCE = "33"
SEND_URL = f"https://waapi.app/api/v1/instances/{INSTANCE}/client/action/send-message"


def waapi_event(body="hola", sender="51999999999@c.us", message_id="ABC123", from_me=False, kind="chat", event="message", instance=INSTANCE):
    return {"event": event, "instanceId": instance, "data": {"message": {
        "id": {"id": message_id, "_serialized": f"false_{sender}_{message_id}", "fromMe": from_me},
        "from": sender, "to": "51900000000@c.us", "body": body, "type": kind, "fromMe": from_me}}}


def accepted():
    response = Mock(); response.raise_for_status.return_value = None
    response.json.return_value = {"data": {"status": "success"}}
    return response


class WaapiWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db"); os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        app.OPENAI_API_KEY = ""; app.user_sessions.clear()
        self.patches = [patch.object(app, "WAAPI_TOKEN", "waapi-token"), patch.object(app, "WAAPI_INSTANCE_ID", INSTANCE), patch.object(app, "WAAPI_WEBHOOK_SECRET", SECRET)]
        for p in self.patches: p.start()
        self.client = app.app.test_client()
    def tearDown(self):
        for p in self.patches: p.stop()
        self.tempdir.cleanup()
    def post(self, payload, secret=SECRET):
        return self.client.post(f"/webhook/waapi/{secret}", json=payload)

    def test_wrong_secret_is_rejected(self):
        with patch.object(app.requests, "post") as post: result = self.post(waapi_event(), secret="wrong")
        self.assertEqual(result.status_code, 401); post.assert_not_called()

    def test_unconfigured_waapi_is_unavailable(self):
        with patch.object(app, "WAAPI_WEBHOOK_SECRET", ""), patch.object(app.requests, "post") as post: result = self.post(waapi_event(), secret="")
        self.assertIn(result.status_code, (404, 503)); post.assert_not_called()

    def test_wrong_instance_is_rejected(self):
        with patch.object(app.requests, "post") as post: result = self.post(waapi_event(instance="99"))
        self.assertEqual(result.status_code, 401); post.assert_not_called()

    def test_text_message_is_answered_through_waapi(self):
        with patch.object(app.requests, "post", return_value=accepted()) as post: result = self.post(waapi_event())
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], SEND_URL)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer waapi-token")
        self.assertEqual(post.call_args.kwargs["json"]["chatId"], "51999999999@c.us")
        self.assertTrue(post.call_args.kwargs["json"]["message"])
        lead = app.get_lead_store().get_lead("51999999999"); self.assertEqual(lead["status"], "en_calificacion")

    def test_reply_hides_internal_references(self):
        with patch.object(app, "progressive_reply", return_value=("Te muestro C2-2B74EDF3CEE0 en Surco", None, "qualification", None)), patch.object(app.requests, "post", return_value=accepted()) as post:
            self.post(waapi_event())
        self.assertNotIn("C2-2B74EDF3CEE0", post.call_args.kwargs["json"]["message"])

    def test_own_messages_are_ignored(self):
        with patch.object(app.requests, "post") as post: result = self.post(waapi_event(from_me=True))
        self.assertEqual(result.get_json()["status"], "ignored"); post.assert_not_called()

    def test_groups_status_and_channels_are_ignored(self):
        for sender in ("120363000000000000@g.us", "status@broadcast", "120363000000000000@newsletter"):
            with patch.object(app.requests, "post") as post: result = self.post(waapi_event(sender=sender, message_id=sender[:6]))
            self.assertEqual(result.get_json()["status"], "ignored"); post.assert_not_called()

    def test_non_text_and_other_events_are_ignored(self):
        for payload in (waapi_event(kind="ptt"), waapi_event(event="message_ack"), waapi_event(body="   ")):
            with patch.object(app.requests, "post") as post: result = self.post(payload)
            self.assertEqual(result.get_json()["status"], "ignored"); post.assert_not_called()

    def test_retried_delivery_is_not_answered_twice(self):
        with patch.object(app.requests, "post", return_value=accepted()) as post:
            self.post(waapi_event(message_id="DUP1")); duplicate = self.post(waapi_event(message_id="DUP1"))
        self.assertEqual(duplicate.get_json(), {"status": "duplicate", "processed": 0}); self.assertEqual(post.call_count, 1)

    def test_lid_sender_is_answered_on_the_same_chat(self):
        with patch.object(app.requests, "post", return_value=accepted()) as post: self.post(waapi_event(sender="123456789012345@lid", message_id="LID1"))
        self.assertEqual(post.call_args.kwargs["json"]["chatId"], "123456789012345@lid")

    def test_waapi_error_status_is_logged_without_failing_the_webhook(self):
        rejected = Mock(); rejected.raise_for_status.return_value = None; rejected.json.return_value = {"data": {"status": "error", "message": "chat not found"}}
        with patch.object(app.requests, "post", return_value=rejected), self.assertLogs("arenz", level="WARNING") as logs:
            result = self.post(waapi_event(message_id="ERR1"))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(any("WAAPI" in line for line in logs.output))

    def test_meta_webhook_keeps_replying_through_graph(self):
        import hashlib, hmac, json
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [{"id": "wamid-meta", "from": "51988888888", "type": "text", "text": {"body": "hola"}}]}}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode(); digest = hmac.new(app.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        response = Mock(); response.raise_for_status.return_value = None
        with patch.object(app.requests, "post", return_value=response) as post:
            self.client.post("/webhook", data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"})
        self.assertTrue(post.call_args.args[0].startswith("https://graph.facebook.com/"))


class HealthByProviderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db"); os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        self.client = app.app.test_client()
    def tearDown(self): self.tempdir.cleanup()

    def test_waapi_provider_needs_waapi_credentials_not_meta(self):
        with patch.object(app, "WHATSAPP_PROVIDER", "waapi"), patch.object(app, "WHATSAPP_TOKEN", ""), patch.object(app, "APP_SECRET", ""), \
             patch.object(app, "WAAPI_TOKEN", "t"), patch.object(app, "WAAPI_INSTANCE_ID", "1"), patch.object(app, "WAAPI_WEBHOOK_SECRET", "s"):
            self.assertEqual(self.client.get("/health").get_json()["status"], "ok")
        with patch.object(app, "WHATSAPP_PROVIDER", "waapi"), patch.object(app, "WAAPI_TOKEN", ""):
            self.assertEqual(self.client.get("/health").get_json()["status"], "degraded")

    def test_meta_is_the_default_provider(self):
        self.assertEqual(app.WHATSAPP_PROVIDER, "meta")


if __name__ == "__main__":
    unittest.main()
