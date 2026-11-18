"""Frase cálida de la IA antes de las preguntas fijas del flujo (CONVERSACION_IA=on).

13/11: "la conversación inteligente no se nota, parece muy plana". Las preguntas del flujo son textos fijos (así no
se repiten distritos ni se piden datos fuera de orden); ahora la IA puede anteponer una frase breve que reaccione a lo
que dijo el cliente. La pregunta y los datos siguen saliendo del código.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")

import app  # noqa: E402
import banco_conversaciones as banco  # noqa: E402

PRESUPUESTO = "¿Cuál es tu presupuesto máximo aproximado? Puedes indicarlo en soles o en dólares."
DISTRITO = banco.historial("qualification", {}, "nueva búsqueda", "Nueva búsqueda iniciada. ¿En qué distrito o distritos te gustaría tu departamento?")


def turno(historia, cliente, frase, modo="on", **slots):
    observacion = banco.ia("property_search", "", **slots)
    observacion["respuesta_cliente"] = banco.clasificacion("otra")
    observacion["frase_puente"] = frase
    with mock.patch.dict(os.environ, {"CONVERSACION_IA": modo}):
        return banco.ejecutar({"historia": historia, "cliente": cliente, "ia": observacion})


class FrasePuenteTest(unittest.TestCase):
    def test_antes_de_la_pregunta_fija(self):
        r = turno(DISTRITO, "Chorrillos", "Chorrillos es una gran zona, cerca al mar 🌊", districts=["Chorrillos"])
        self.assertEqual(r.visible, f"Chorrillos es una gran zona, cerca al mar 🌊 {PRESUPUESTO}")

    def test_cada_pregunta_del_flujo(self):
        sin_pago = {k: v for k, v in banco.CRITERIOS_SURCO.items() if k != "payment_method"}
        casos = [
            (banco.historial("qualification", {"districts": ["Surco"]}, "Surco", PRESUPUESTO), "500 mil soles",
             {"budget_max": 500000, "currency": "PEN"}, "¿Cuántos dormitorios necesitas?"),
            (banco.historial("qualification", {"districts": ["Surco"], "budget_max": 500000, "currency": "PEN"}, "500 mil soles",
                             "¿Cuántos dormitorios necesitas?"), "3", {"bedrooms": 3}, banco.PREGUNTA_PAGO),
            (banco.historial("qualification", sin_pago, "3", banco.PREGUNTA_PAGO), "al contado", {"payment_method": "contado"},
             "¿para cuándo necesitas el departamento?"),
        ]
        for historia, cliente, slots, pregunta in casos:
            r = turno(historia, cliente, "¡Genial, vamos muy bien! 😊", **slots)
            self.assertTrue(r.visible.startswith("¡Genial, vamos muy bien! 😊 "), (cliente, r.visible))
            self.assertIn(pregunta, r.visible)

    def test_en_el_plazo_reemplaza_el_perfecto_gracias(self):
        r = turno(banco.historial("qualification", {k: v for k, v in banco.CRITERIOS_SURCO.items() if k != "payment_method"},
                                  "3", banco.PREGUNTA_PAGO), "contado", "Pagar al contado te da más poder de negociación 👍",
                  payment_method="contado")
        self.assertTrue(r.visible.startswith("Pagar al contado te da más poder de negociación 👍 Último dato"), r.visible)
        self.assertNotIn("Perfecto, gracias", r.visible)

    def test_se_descarta_si_trae_cifras_promesas_o_preguntas(self):
        for frase in ("Con 500 mil tienes buenas opciones", "Tengo varios departamentos en Chorrillos",
                      "Hay muchas opciones en esa zona", "¿Te gusta el mar?", "Chorrillos tiene 340 unidades",
                      "Un asesor te llamará pronto", "Te muestro las mejores", "Está disponible ya", ""):
            r = turno(DISTRITO, "Chorrillos", frase, districts=["Chorrillos"])
            self.assertEqual(r.visible, PRESUPUESTO, frase)

    def test_no_toca_listados_fichas_ni_correcciones(self):
        # Listado de opciones: sin frase.
        r = turno(banco.H_PREGUNTA_FUTURA, "sí", "¡Excelente elección! 😊")
        self.assertNotIn("Excelente elección", r.visible)
        # Corrección de un dato: el "Perfecto, corregí…" ya es la reacción.
        historia = banco.historial("qualification", {"districts": ["Surco"], "budget_max": 25000, "currency": "USD"},
                                   "25000 dólares", "¿Cuántos dormitorios necesitas?")
        r = turno(historia, "250 mil dólares", "¡Buen presupuesto! 😊", budget_max=250000, currency="USD")
        self.assertTrue(r.visible.startswith("Perfecto, corregí"), r.visible)
        self.assertNotIn("Buen presupuesto", r.visible)

    def test_apagado_no_usa_la_frase(self):
        r = turno(DISTRITO, "Chorrillos", "Chorrillos es una gran zona 🌊", modo="off", districts=["Chorrillos"])
        self.assertEqual(r.visible, PRESUPUESTO)

    def test_no_repite_la_frase_anterior(self):
        historia = banco.historial("qualification", {"districts": ["Surco"]}, "Surco", f"¡Genial, vamos muy bien! 😊 {PRESUPUESTO}")
        r = turno(historia, "500 mil soles", "¡Genial, vamos muy bien! 😊", budget_max=500000, currency="PEN")
        self.assertEqual(r.visible, "¿Cuántos dormitorios necesitas?")

    def test_esquema_pide_la_frase(self):
        self.assertIn("frase_puente", app.OBSERVATION_SCHEMA_IA["required"])
        self.assertNotIn("frase_puente", app.OBSERVATION_SCHEMA["properties"])


if __name__ == "__main__":
    unittest.main()
