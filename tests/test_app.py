import hashlib, hmac, json, os, tempfile, unittest
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(); os.environ["LEADS_DB_PATH"] = os.path.join(self.tempdir.name, "leads.db"); os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
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
    def test_ai_http_error_uses_fallback(self):
        app.OPENAI_API_KEY="not-a-real-key"; response=Mock(); response.status_code=401; error=app.requests.HTTPError(); error.response=response; response.raise_for_status.side_effect=error
        with patch.object(app.requests,"post",return_value=response): self.assertEqual(app.generate_ai_reply("519","hola","base"),"base")
    def test_structured_observation_is_parsed_without_changing_reply(self):
        app.OPENAI_API_KEY="not-a-real-key"; expected={"intent":"property_search","slot_updates":{"operation":"compra","districts":["Miraflores"],"budget_max":200000,"currency":"USD","bedrooms":3,"property_type":"departamento","preferences":[]},"criteria_change":False,"user_question":None,"next_action":"ask_clarification","handoff":False,"assistant_reply":"¿Prefieres nuevo o usado?"}; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value={"output_text":json.dumps(expected)}
        deterministic=app.generate_reply("519","hola")
        with patch.object(app.requests,"post",return_value=response): observation=app.observe_conversation("519","Busco comprar en Miraflores hasta US$200 mil, 3 dormitorios",deterministic)
        self.assertEqual(observation,expected); self.assertEqual(deterministic,"Hola 👋 Soy ARENZ AI.\n\n¿Qué estás buscando?\n1️⃣ Comprar\n2️⃣ Alquilar\n3️⃣ Vender\n4️⃣ Hablar con un asesor")
    def test_structured_observation_reads_output_content_format(self):
        app.OPENAI_API_KEY="not-a-real-key"; expected={"intent":"greeting","slot_updates":{"operation":None,"districts":[],"budget_max":None,"currency":None,"bedrooms":None,"property_type":None,"preferences":[]},"criteria_change":False,"user_question":None,"next_action":"reply","handoff":False,"assistant_reply":"Hola"}; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value={"output":[{"content":[{"type":"output_text","text":json.dumps(expected)}]}]}
        with patch.object(app.requests,"post",return_value=response): self.assertEqual(app.observe_conversation("519","hola","base"),expected)
    def test_empty_structured_observation_logs_safe_shape(self):
        app.OPENAI_API_KEY="not-a-real-key"; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value={"status":"completed","output":[{"content":[{"type":"refusal","refusal":"contenido-no-registrado"}]}]}
        with self.assertLogs("arenz", level="WARNING") as logs:
            with patch.object(app.requests,"post",return_value=response): self.assertIsNone(app.observe_conversation("519","hola","base"))
        output = "\n".join(logs.output)
        self.assertIn("output_present=True", output)
        self.assertIn("content_types=refusal", output)
        self.assertIn("refusal_present=True", output)
        self.assertNotIn("contenido-no-registrado", output)
    def test_invalid_structured_observation_is_ignored(self):
        app.OPENAI_API_KEY="not-a-real-key"; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value={"output_text":"{\"intent\":\"unknown\"}"}
        with patch.object(app.requests,"post",return_value=response): self.assertIsNone(app.observe_conversation("519","hola","base"))
    def test_supabase_lead_upsert(self):
        os.environ["SUPABASE_URL"]="https://project.supabase.co"; os.environ["SUPABASE_KEY"]="test-supabase-key"; response=Mock(); response.raise_for_status.return_value=None
        with patch.object(app.requests,"post",return_value=response) as post:
            app.get_lead_store().upsert_lead("51999999999", {"step":"done", "intent":"compra", "district":"Miraflores", "budget":"US$ 200000", "bedrooms":"3"}, "hola", "respuesta")
        self.assertEqual(post.call_args.args[0],"https://project.supabase.co/rest/v1/leads")
        self.assertEqual(post.call_args.kwargs["json"]["phone"],"51999999999"); self.assertEqual(post.call_args.kwargs["json"]["intent"],"compra"); self.assertEqual(post.call_args.kwargs["json"]["district"],"Miraflores"); self.assertEqual(post.call_args.kwargs["json"]["status"],"pendiente_asesor")
    def test_health_with_supabase_store(self):
        os.environ["SUPABASE_URL"]="https://project.supabase.co"; os.environ["SUPABASE_KEY"]="test-supabase-key"
        self.assertEqual(self.client.get("/health").get_json()["status"],"ok")
    def test_conversation_memory_persists_session_and_turns(self):
        os.environ["SUPABASE_URL"]="https://project.supabase.co"; os.environ["SUPABASE_KEY"]="test-supabase-key"; response=Mock(); response.raise_for_status.return_value=None; observation={"intent":"greeting"}
        with patch.object(app.requests,"post",return_value=response) as post: app.get_conversation_memory_store().record_observation("519","wamid-1","hola","respuesta",observation)
        self.assertEqual(post.call_count,2); self.assertIn("conversation_sessions?on_conflict=phone",post.call_args_list[0].args[0]); self.assertIn("conversation_messages?on_conflict=message_key",post.call_args_list[1].args[0]); self.assertEqual(post.call_args_list[1].kwargs["json"][0]["message_key"],"in:wamid-1")
    def test_supabase_duplicate_claim(self):
        os.environ["SUPABASE_URL"]="https://project.supabase.co"; os.environ["SUPABASE_KEY"]="test-supabase-key"; response=Mock(); response.raise_for_status.return_value=None; response.json.return_value=[]
        with patch.object(app.requests,"post",return_value=response) as post: self.assertFalse(app.get_lead_store().claim_message("wamid-duplicate"))
        self.assertEqual(post.call_args.args[0],"https://project.supabase.co/rest/v1/processed_messages?on_conflict=message_id")

    def test_progressive_controller_keeps_all_criteria_from_one_message(self):
        observation={"intent":"property_search","slot_updates":{"operation":"compra","districts":["Miraflores"],"budget_max":180000,"currency":"USD","bedrooms":3,"property_type":"departamento","preferences":[]},"handoff":False}
        reply, criteria, stage, _ = app.progressive_reply(None, observation, "fallback")
        self.assertEqual(stage,"qualified"); self.assertEqual(criteria["districts"],["Miraflores"]); self.assertIn("Miraflores",reply); self.assertNotIn("¿En qué distrito",reply)

    def test_progressive_controller_updates_criteria_without_losing_previous(self):
        previous={"state":{"criteria":{"operation":"compra","districts":["Miraflores"],"budget_max":180000,"currency":"USD","bedrooms":3,"property_type":"departamento"}}}
        observation={"intent":"change_criteria","slot_updates":{"operation":None,"districts":["Surco"],"budget_max":220000,"currency":"USD","bedrooms":None,"property_type":None,"preferences":["balcón"]},"handoff":False}
        _, criteria, stage, _ = app.progressive_reply(previous, observation, "fallback")
        self.assertEqual(stage,"qualified"); self.assertEqual(criteria["districts"],["Surco"]); self.assertEqual(criteria["budget_max"],220000); self.assertEqual(criteria["bedrooms"],3)

    def test_general_question_and_handoff_do_not_reset_conversation(self):
        previous={"state":{"criteria":{"operation":"compra","districts":["Miraflores"]}}}
        question={"intent":"general_question","slot_updates":{},"handoff":False,"assistant_reply":"Claro, puedo ayudarte con esa consulta."}
        reply, criteria, stage, _ = app.progressive_reply(previous, question, "fallback")
        self.assertEqual(stage,"conversation"); self.assertEqual(criteria["operation"],"compra"); self.assertIn("consulta",reply)
        handoff={"intent":"human_handoff","slot_updates":{},"handoff":True}
        _, _, stage, _ = app.progressive_reply(previous, handoff, "fallback")
        self.assertEqual(stage,"handoff")

    def test_progressive_controller_uses_deterministic_fallback_when_observation_fails(self):
        reply, criteria, stage, _ = app.progressive_reply({"state":{"criteria":{"operation":"compra"}}}, None, "fallback seguro")
        self.assertEqual((reply,criteria,stage),("fallback seguro",{"operation":"compra"},"fallback"))

    def test_memory_session_reconstructs_criteria_after_restart(self):
        previous={"state":{"criteria":{"operation":"compra","districts":["Miraflores"],"budget_max":180000,"currency":"USD","bedrooms":3,"property_type":"departamento"}}}
        self.assertEqual(app.conversation_state(previous)["bedrooms"],3)

if __name__ == "__main__": unittest.main()
