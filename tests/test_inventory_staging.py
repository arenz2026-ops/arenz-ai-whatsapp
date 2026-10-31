import json, os, unittest
from unittest.mock import Mock, patch
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app


def resp(ok=True, payload=None):
    r = Mock(); r.ok = ok; r.status_code = 200 if ok else 500; r.json.return_value = payload if payload is not None else []; return r


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.env = patch.dict(os.environ, {"SUPABASE_URL": "https://db.example", "SUPABASE_KEY": "k"}); self.env.start()

    def tearDown(self):
        self.env.stop()

    def post(self, body):
        return self.client.post("/admin/inventario-staging", data=json.dumps(body), content_type="text/plain")

    @patch("inventory_staging.requests.post")
    @patch("inventory_staging.requests.get")
    def test_valid_token_stages_rows(self, get, post):
        get.return_value = resp(payload=[{"token": "x" * 32}]); post.return_value = resp()
        r = self.post({"token": "x" * 32, "source": "lista_precios", "rows": [["a", 1]]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"], [{"source": "lista_precios", "r": ["a", 1]}])

    @patch("inventory_staging.requests.post")
    @patch("inventory_staging.requests.get")
    def test_unknown_token_is_refused(self, get, post):
        get.return_value = resp(payload=[])
        r = self.post({"token": "y" * 32, "source": "lista_precios", "rows": [["a"]]})
        self.assertEqual(r.status_code, 403); post.assert_not_called()

    @patch("inventory_staging.requests.get")
    def test_unknown_source_is_refused(self, get):
        r = self.post({"token": "x" * 32, "source": "otra", "rows": [["a"]]})
        self.assertEqual(r.status_code, 400); get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
