import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app
import advisor_notify as an

CRITERIA = {"operation": "compra", "property_type": "departamento", "bedrooms": 2, "districts": ["Surco", "Lince"],
            "budget_max": 250000, "currency": "USD", "payment_method": "credito", "credit_status": "en_tramite",
            "purchase_timeline": "en 3 meses"}
HANDOFF_OBSERVATION = {"intent": "human_handoff", "slot_updates": {"operation": None, "districts": [], "budget_max": None,
                       "currency": None, "bedrooms": None, "property_type": None, "parking_required": None, "preferences": []},
                       "criteria_change": False, "user_question": None, "next_action": "handoff", "handoff": True,
                       "assistant_reply": "Te paso con un asesor."}


def ok():
    response = Mock(); response.raise_for_status.return_value = None; return response


class AdvisorMessageTests(unittest.TestCase):
    def test_phones_are_normalized_and_deduplicated(self):
        with patch.dict(os.environ, {"ADVISOR_PHONE": "+51 979 300 318, 979300318 ,51996353549,"}):
            self.assertEqual(an.advisor_phones(), ["51979300318", "51996353549"])

    def test_no_phone_means_no_notice(self):
        post = Mock()
        with patch.dict(os.environ, {"ADVISOR_PHONE": ""}):
            self.assertEqual(an.notify_advisors(post, "51911111111", {"request_type": "advisor", "property_reference": None}, CRITERIA, "hola"), 0)
        post.assert_not_called()

    def test_text_notice_carries_the_five_filters_and_contact_link(self):
        body = an.advisor_text("51911111111", {"request_type": "visit", "property_reference": "ARZ-000001"}, CRITERIA, "Quiero visitar la ARZ-000001")
        for expected in ("Pidió una visita", "+51911111111", "https://wa.me/51911111111", "Inmueble: ARZ-000001",
                         "Qué: compra · departamento · 2 dorm.", "Dónde: Surco, Lince", "Cuánto: USD 250,000",
                         "Cómo: crédito hipotecario · crédito en trámite", "Cuándo: en 3 meses", "Quiero visitar"):
            self.assertIn(expected, body)

    def test_missing_filters_are_marked_not_invented(self):
        body = an.advisor_text("51911111111", {"request_type": "advisor", "property_reference": None}, {"operation": "alquiler"}, "")
        self.assertIn("Dónde: no indicado", body); self.assertIn("Cuánto: no indicado", body); self.assertNotIn("Inmueble:", body)

    def test_template_used_when_configured_and_params_are_meta_safe(self):
        post = Mock()
        with patch.dict(os.environ, {"ADVISOR_PHONE": "979300318", "ADVISOR_TEMPLATE": "aviso_lead", "ADVISOR_TEMPLATE_LANGUAGE": "es_PE"}):
            sent = an.notify_advisors(post, "51911111111", {"request_type": "callback", "property_reference": None},
                                      {**CRITERIA, "preferences": ["vista\nal parque"]}, "llámame")
        self.assertEqual(sent, 1)
        payload = post.call_args.args[0]
        self.assertEqual(payload["type"], "template"); self.assertEqual(payload["to"], "51979300318")
        self.assertEqual(payload["template"]["name"], "aviso_lead"); self.assertEqual(payload["template"]["language"]["code"], "es_PE")
        params = [p["text"] for p in payload["template"]["components"][0]["parameters"]]
        self.assertEqual(params[:3], ["+51911111111", "Pidió que lo llamen", "sin referencia"])
        self.assertTrue(all("\n" not in p and "    " not in p for p in params))

    def test_one_failing_advisor_does_not_stop_the_others(self):
        post = Mock(side_effect=[RuntimeError("rejected"), None])
        with patch.dict(os.environ, {"ADVISOR_PHONE": "51900000001,51900000002", "ADVISOR_TEMPLATE": ""}):
            self.assertEqual(an.notify_advisors(post, "51911111111", {"request_type": "advisor", "property_reference": None}, CRITERIA, "x"), 1)
        self.assertEqual(post.call_count, 2)

    def test_client_is_never_notified_as_advisor(self):
        post = Mock()
        with patch.dict(os.environ, {"ADVISOR_PHONE": "51911111111", "ADVISOR_TEMPLATE": ""}):
            self.assertEqual(an.notify_advisors(post, "51911111111", {"request_type": "advisor", "property_reference": None}, CRITERIA, "x"), 0)
        post.assert_not_called()


class AdvisorWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db")
        os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
        app.APP_SECRET = "test-app-secret"; app.WHATSAPP_TOKEN = "test-whatsapp-token"; app.PHONE_NUMBER_ID = "123456"
        app.OPENAI_API_KEY = "not-a-real-key"; app.user_sessions.clear(); self.client = app.app.test_client()
        self.env = patch.dict(os.environ, {"ADVISOR_PHONE": "51979300318", "ADVISOR_TEMPLATE": ""}); self.env.start()

    def tearDown(self):
        self.env.stop(); self.tempdir.cleanup(); app.OPENAI_API_KEY = ""

    def post_message(self, body, message_id, observation, graph_side_effect):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
            {"id": message_id, "from": "51911111111", "type": "text", "text": {"body": body}}]}}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(app.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()}
        openai = ok(); openai.json.return_value = {"output_text": json.dumps(observation)}
        with patch.object(app.requests, "post", side_effect=[openai, *graph_side_effect]) as post:
            result = self.client.post("/webhook", data=raw, headers=headers)
        return result, post

    def test_handoff_notifies_the_advisor_after_the_client_reply(self):
        result, post = self.post_message("Quiero hablar con un asesor", "wamid-adv-1", HANDOFF_OBSERVATION, [ok(), ok()])
        self.assertEqual(result.status_code, 200)
        client_call, advisor_call = post.call_args_list[1], post.call_args_list[2]
        self.assertEqual(client_call.kwargs["json"]["to"], "51911111111")
        self.assertEqual(advisor_call.kwargs["json"]["to"], "51979300318")
        self.assertIn("Pidió hablar con un asesor", advisor_call.kwargs["json"]["text"]["body"])

    def test_advisor_failure_does_not_break_the_turn(self):
        failing = ok(); failing.raise_for_status.side_effect = app.requests.HTTPError("rejected")
        failing.json.return_value = {"error": {"code": 131047}}
        result, post = self.post_message("Quiero hablar con un asesor", "wamid-adv-2", HANDOFF_OBSERVATION, [ok(), failing])
        self.assertEqual(result.get_json(), {"status": "received", "processed": 1})
        self.assertEqual(post.call_count, 3)

    def test_ordinary_turn_sends_no_notice(self):
        observation = {**HANDOFF_OBSERVATION, "intent": "property_search", "next_action": "ask_clarification", "handoff": False,
                       "slot_updates": {**HANDOFF_OBSERVATION["slot_updates"], "operation": "compra"},
                       "assistant_reply": "Perfecto, ¿en qué distrito buscas?"}
        result, post = self.post_message("Quiero comprar", "wamid-adv-3", observation, [ok()])
        self.assertEqual(result.status_code, 200); self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
