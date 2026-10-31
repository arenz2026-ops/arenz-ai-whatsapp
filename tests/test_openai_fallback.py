import os, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app


def resp(status, payload):
    r = Mock(); r.status_code = status; r.ok = status < 400; r.json.return_value = payload; return r


class OpenAIFallbackTests(unittest.TestCase):
    def setUp(self):
        self.saved = app.OPENAI_MODEL

    def tearDown(self):
        app.OPENAI_MODEL = self.saved

    @patch("app.requests.post")
    def test_retired_model_switches_to_fallback(self, post):
        post.side_effect = [resp(404, {"error": {"code": "model_not_found"}}), resp(200, {"ok": True})]
        r = app.post_openai_response({"model": "gpt-4.1-nano", "input": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], app.OPENAI_FALLBACK_MODEL)
        self.assertEqual(app.OPENAI_MODEL, app.OPENAI_FALLBACK_MODEL)

    @patch("app.requests.post")
    def test_other_errors_are_not_retried(self, post):
        post.return_value = resp(401, {"error": {"code": "invalid_api_key"}})
        app.post_openai_response({"model": "gpt-4.1-nano", "input": "x"})
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
