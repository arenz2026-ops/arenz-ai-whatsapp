"""Clasificaciones que devolvió la IA real (gpt-4.1-mini) en la prueba del 02/11 con scripts/probar_ia_real.py.

Cada una hacía que el bot actuara mal con CONVERSACION_IA=on. Aquí se reproducen tal cual y el
comportamiento final debe cumplir las mismas expectativas del banco de conversaciones.
"""
import copy
import unittest

import app
import banco_conversaciones as banco
from test_conversacion_dificil import ejecutar

# id del caso -> (clasificación real (tipo, pedido, opción, dato), acciones de criterio que añadió la IA[, otros campos])
REALES = {
    "neg_todavia_no_al_asesor": (("rechaza", "asesor"), []),
    "neg_nel_al_brochure": (("rechaza", "asesor"), []),
    "neg_nop_a_mas_fotos": (("rechaza", "fotos"), []),
    "no_pero_tiene_cochera": (("rechaza", "ninguno", None, "estacionamiento"), []),
    "dato_cuanto_cuesta": (("otra", "ninguno", None, "precio"), []),
    "cambio_no_mejor_la_2": (("pide_otra_cosa", "opcion", 2), [{"action": "UPDATE", "field": "property_type", "values": ["departamento"]}]),
    "cambio_no_mejor_la_2#otras": (("pide_otra_cosa", "otras_opciones", 2), []),
    "cambio_no_mejor_la_2#preferencia": (("pide_otra_cosa", "opcion", 2), [{"action": "UPDATE", "field": "preferences", "values": ["opcion_2"]}]),
    "neg_no_gracias_asi_esta_bien_a_la_visita": (("otra", "asesor"), [], {"handoff": True}),
    "neg_no_gracias_asi_esta_bien_a_la_visita#rechaza": (("rechaza", "ninguno"), [], {"handoff": True}),
    "neg_todavia_no_al_asesor#acepta": (("acepta", "asesor"), []),
    "jerga_ta_caro": (("pide_otra_cosa", "cambiar_criterios", None, "precio"), [{"action": "UPDATE", "field": "budget_max", "values": [289460]}],
                      {"slot_updates": {**banco.ia()["slot_updates"], "budget_max": 289460, "currency": "PEN"}}),
    # Variantes que ya funcionaban y deben seguir funcionando.
    "no_pero_mandame_mas_fotos": (("rechaza", "fotos"), []),
    "no_pero_muestrame_otra": (("rechaza", "otras_opciones"), []),
    "no_pero_muestrame_otra#nueva": (("rechaza", "otras_opciones"), [{"action": "REMOVE", "field": "districts", "values": ["Pueblo Libre"]}],
                                     {"intent": "new_search"}),
}


class ClasificacionRealTest(unittest.TestCase):
    def test_acciones_finales_correctas(self):
        for caso_id, (clase, acciones, *extra) in REALES.items():
            with self.subTest(caso=caso_id):
                caso = copy.deepcopy(next(c for c in banco.CASOS if c["id"] == caso_id.split("#")[0]))
                caso["ia"]["respuesta_cliente"] = banco.clasificacion(*clase)
                caso["ia"]["criteria_actions"] = acciones
                caso["ia"].update(*extra)
                resultado = ejecutar(caso, "on")
                fallas = [descripcion for descripcion, cumple in caso["espera"] if not cumple(resultado, caso)]
                self.assertEqual(fallas, [], f"{resultado!r}")


class CorreccionTest(unittest.TestCase):
    def test_rechazo_simple(self):
        for texto in ("no", "nop", "nel", "todavía no", "No, gracias.", "no gracias, así está bien", "por ahora no"):
            self.assertTrue(app.plain_refusal(texto), texto)
        for texto in ("no, pero mándame más fotos", "no gracias, pero ¿tiene cochera?", "no, mejor la 2", "no, ninguna, ¿otra opción?",
                      "sí", "dale"):
            self.assertFalse(app.plain_refusal(texto), texto)

    def test_un_no_simple_pierde_el_pedido_de_la_oferta(self):
        clase = app.corrected_reply_class(banco.clasificacion("rechaza", "asesor"), "todavía no")
        self.assertEqual((clase["tipo"], clase["pedido"]), ("rechaza", "ninguno"))

    def test_pregunta_por_un_dato_es_un_pedido(self):
        for clase in (banco.clasificacion("rechaza", dato="estacionamiento"), banco.clasificacion("otra", dato="precio")):
            corregida = app.corrected_reply_class(clase, "¿y cuánto cuesta?")
            self.assertEqual((corregida["tipo"], corregida["pedido"]), ("pide_otra_cosa", "dato_unidad"))

    def test_presupuesto_sin_cifra_en_el_mensaje_se_descarta(self):
        observacion = {"criteria_actions": [{"action": "UPDATE", "field": "budget_max", "values": [289460]}],
                       "slot_updates": {"budget_max": 289460}}
        limpia = app.without_invented_budget(observacion, "ta caro")
        self.assertEqual(limpia["criteria_actions"], []); self.assertIsNone(limpia["slot_updates"]["budget_max"])
        self.assertEqual(app.without_invented_budget(observacion, "mejor hasta 250 mil"), observacion)
        # El presupuesto ya conocido se conserva (una búsqueda nueva lo necesita).
        previous = banco.historial("qualified", banco.CRITERIOS_PL, "sí", banco.LISTADO_PL)
        conocido = app.without_invented_budget({"criteria_actions": [], "slot_updates": {"budget_max": 400000}}, "¿tienes en Surco?", previous)
        self.assertEqual(conocido["slot_updates"]["budget_max"], 400000)
        cambiado = app.without_invented_budget(observacion, "ta caro", previous)
        self.assertEqual(cambiado["slot_updates"]["budget_max"], 400000)

    def test_acciones_que_repiten_un_criterio_no_son_un_cambio(self):
        criterios = dict(banco.CRITERIOS_PL)
        self.assertFalse(app.changes_criteria(criterios, [{"action": "UPDATE", "field": "property_type", "values": ["departamento"]}]))
        self.assertTrue(app.changes_criteria(criterios, [{"action": "UPDATE", "field": "districts", "values": ["Surco"]}]))
        self.assertFalse(app.changes_criteria(criterios, []))


if __name__ == "__main__":
    unittest.main()
