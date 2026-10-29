import os, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app
import embedded_signup as es


def resp(ok=True, payload=None):
    r = Mock(); r.ok = ok; r.json.return_value = payload or {}; r.raise_for_status = Mock(); return r


class EmbeddedSignupTests(unittest.TestCase):
    def setUp(self):
        es._CACHE.update(at=0.0, row=None)
        self.client = app.app.test_client()
        self.env = patch.dict(os.environ, {"SUPABASE_URL": "https://db.example", "SUPABASE_KEY": "k"})
        self.env.start()

    def tearDown(self):
        self.env.stop(); es._CACHE.update(at=0.0, row=None)

    def test_page_embeds_app_and_config_ids(self):
        body = self.client.get("/conectar-whatsapp").get_data(as_text=True)
        self.assertIn(es.META_APP_ID, body); self.assertIn(es.ES_CONFIG_ID, body)
        self.assertIn("whatsapp_business_app_onboarding", body)

    @patch("embedded_signup.requests.post")
    @patch("embedded_signup.requests.get")
    def test_valid_signup_subscribes_and_stores(self, get, post):
        get.side_effect = [resp(payload={"access_token": "biz-token"}),
                           resp(payload={"owner_business_info": {"id": es.ALLOWED_BUSINESS_ID}})]
        post.side_effect = [resp(), resp()]
        r = self.client.post("/conectar-whatsapp/callback", json={"code": "c", "waba_id": "w1", "phone_number_id": "p1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/w1/subscribed_apps", post.call_args_list[0].args[0])
        stored = post.call_args_list[1].kwargs["json"]
        self.assertEqual((stored["phone_number_id"], stored["access_token"]), ("p1", "biz-token"))

    @patch("embedded_signup.requests.post")
    @patch("embedded_signup.requests.get")
    def test_foreign_business_is_refused(self, get, post):
        get.side_effect = [resp(payload={"access_token": "t"}), resp(payload={"owner_business_info": {"id": "999"}})]
        r = self.client.post("/conectar-whatsapp/callback", json={"code": "c", "waba_id": "w", "phone_number_id": "p"})
        self.assertEqual(r.status_code, 400); post.assert_not_called()

    def test_missing_fields_rejected(self):
        r = self.client.post("/conectar-whatsapp/callback", json={"code": "c"})
        self.assertEqual(r.status_code, 400)

    @patch("embedded_signup.requests.get")
    def test_stored_connection_overrides_env(self, get):
        get.return_value = resp(payload=[{"phone_number_id": "p9", "access_token": "t9", "waba_id": "w"}])
        self.assertEqual(es.active_credentials("env-t", "env-p"), ("t9", "p9"))

    def test_env_used_without_supabase(self):
        with patch.dict(os.environ, {"SUPABASE_URL": ""}):
            self.assertEqual(es.active_credentials("env-t", "env-p"), ("env-t", "env-p"))


if __name__ == "__main__":
    unittest.main()
