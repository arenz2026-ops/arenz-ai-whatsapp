import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db")
        app.VERIFY_TOKEN="test-verify-token"; app.WHATSAPP_TOKEN="test-whatsapp-token"; app.PHONE_NUMBER_ID="123456"; app.APP_SECRET="test-app-secret"; app.OPENAI_API_KEY=""; app.user_sessions.clear(); self.client=app.app.test_client()
    def tearDown(self): self.tempdir.cleanup()
    def signed(self, payload):
        raw=json.dumps(payload,separators=(",",":")).encode(); digest=hmac.new(app.APP_SECRET.encode(),raw,hashlib.sha256).hexdigest()
        return raw,{"Content-Type":"application/json","X-Hub-Signature-256":f"sha256={digest}"}
    def test_get_verification(self):
        response=self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=ok")
        self.assertEqual((response.status_code,response.get_data(as_text=True)),(200,"ok")); self.assertEqual(self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=ok").status_code,403)
    def test_health(self): self.assertEqual(self.client.get("/health").get_json()["status"],"ok")
    def test_invalid_signature(self): self.assertEqual(self.client.post("/webhook",json={}).status_code,401)
    def test_post_graph_mock_and_lead(self):
        payload={"object":"whatsapp_business_account","entry":[{"changes":[{"value":{"messages":[{"id":"wamid-1","from":"51999999999","type":"text","text":{"body":"hola"}}]}}]}]}; raw,headers=self.signed(payload); response=Mock(); response.raise_for_status.return_value=None
        with patch.object(app.requests,"post",return_value=response) as post: result=self.client.post("/webhook",data=raw,headers=headers)
        self.assertEqual(result.get_json(),{"status":"received","processed":1}); self.assertEqual(post.call_count,1); lead=app.get_lead_store().get_lead("51999999999"); self.assertEqual(lead["status"],"en_calificacion"); self.assertIn("consulta inmobiliaria",lead["interest"])
    def test_duplicate_is_not_sent_twice(self):
        payload={"object":"whatsapp_business_account","entry":[{"changes":[{"value":{"messages":[{"id":"wamid-2","from":"51999999999","type":"text","text":{"body":"hola"}}]}}]}]}; raw,headers=self.signed(payload); response=Mock(); response.raise_for_status.return_value=None
        with patch.object(app.requests,"post",return_value=response) as post: self.client.post("/webhook",data=raw,headers=headers); duplicate=self.client.post("/webhook",data=raw,headers=headers)
        self.assertEqual(duplicate.get_json(),{"status":"duplicate","processed":0}); self.assertEqual(post.call_count,1)
    def test_ai_simulated(self):
        app.OPENAI_API_KEY="not-a-real-key"; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value={"output_text":"Respuesta IA"}
        with patch.object(app.requests,"post",return_value=response) as post: self.assertEqual(app.generate_ai_reply("519","hola","base"),"Respuesta IA")
        self.assertEqual(post.call_args.args[0],"https://api.openai.com/v1/responses")

if __name__ == "__main__": unittest.main()
