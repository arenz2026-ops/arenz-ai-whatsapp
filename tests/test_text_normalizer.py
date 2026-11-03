import os, unittest
os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")
import app
from text_normalizer import normalize_client_text as n

CASES = {
    "Jesus mria": "Jesús María", "miraflorez": "Miraflores", "En chorillos": "Chorrillos", "pueblo libr": "Pueblo Libre",
    "san isidro o miraflores": "San Isidro y Miraflores", "Hasta 1.5millones de soles tengo": "Hasta 1500000 soles tengo",
    "300k dolares": "300000 dólares", "medio millon de soles": "500000 soles", "dos dormitorios": "2 dormitorios",
    "tres": "3", "sii": "Sí", "okey": "Sí", "Ahora": "Lo antes posible", "imediata": "inmediata",
    # Respuestas cortas mal tipeadas a "¿para cuándo?" (prueba real del 02/11: "Ahorq").
    "Ahorq": "Lo antes posible", "ahroa": "Lo antes posible", "urjente": "Lo antes posible",
    "Quiero ver los otrs": "Quiero ver los otros", "mandame fotitos porfa": "mándame fotos por favor",
    "kontado": "contado", "quiero un depa de 3 cuartos en surquiyo": "quiero un departamento de 3 dormitorios en Surquillo",
    "en la molina o en surco": "en La Molina o en Surco", "Surko, chrrills, once": "Surco y Chorrillos y Lince",
    "500cocos": "500 dólares", "Al montado": "Al contado", "Al toque toke": "lo antes posible", "nesecito hablar con un acesor": "necesito hablar con un asesor",
}
UNCHANGED = ["Surco", "3", "80 m2", "Ya", "departamento de estreno", "dame la ficha de la opcion 2", "450 mil soles",
             "Quiero que me muestres otro departamento", "vista exterior",
             "ascensor", "contacto", "Estoy evaluando"]


class NormalizerTests(unittest.TestCase):
    def test_cases(self):
        for raw, expected in CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(n(raw), expected)

    def test_clean_text_is_left_alone(self):
        for raw in UNCHANGED:
            with self.subTest(raw=raw):
                self.assertEqual(n(raw), raw)

    def test_misspelled_money_reaches_the_budget(self):
        obs = app.with_explicit_operation({"intent": "change_criteria", "slot_updates": {}}, n("Hasta 1.5millones de soles"))
        self.assertEqual(obs["slot_updates"]["currency"], "PEN")
        self.assertEqual(obs["slot_updates"]["budget_max"], 1500000)

    def test_cocos_are_thousands_of_dollars(self):
        obs = app.with_explicit_operation({"intent": "change_criteria", "slot_updates": {}}, n("500cocos"))
        self.assertEqual((obs["slot_updates"]["currency"], obs["slot_updates"]["budget_max"]), ("USD", 500000))

    def test_unknown_word_is_never_a_district(self):
        self.assertEqual(app.validated_slot_updates({"districts": ["Surco", "Xyzw"]})["districts"], ["Surco"])

    def test_misspelled_district_from_model_is_canonical(self):
        self.assertEqual(app.validated_slot_updates({"districts": ["miraflorez", "jesus mria"]})["districts"],
                         ["Miraflores", "Jesús María"])

    def test_more_and_affirmative_rules_see_cleaned_text(self):
        self.assertTrue(app.is_more_request(n("muestrame los otrs")))
        self.assertTrue(app.AFFIRMATIVE_PATTERN.match(n("siii").casefold()))
        self.assertTrue(app.PHOTO_WORDS.search(n("pasame fotitos")))


if __name__ == "__main__":
    unittest.main()
