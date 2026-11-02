"""Banco de conversaciones difíciles (ver tests/banco_conversaciones.py) e interruptor CONVERSACION_IA."""
import os
import unittest
from unittest import mock

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")

import app  # noqa: E402
import banco_conversaciones as banco  # noqa: E402


class InterruptorConversacionIATest(unittest.TestCase):
    def test_apagado_por_defecto(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONVERSACION_IA", None)
            self.assertFalse(app.conversacion_ia_activa())

    def test_valores_reconocidos(self):
        for valor, esperado in (("on", True), ("ON", True), (" 1 ", True), ("true", True), ("sí", True),
                                ("off", False), ("0", False), ("", False), ("no", False), ("cualquiera", False)):
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": valor}):
                self.assertEqual(app.conversacion_ia_activa(), esperado, valor)

    def test_etapa_1_encendido_no_cambia_nada_todavia(self):
        """En la etapa 1 el interruptor solo existe; on y off deben responder igual en todo el banco."""
        for caso in banco.CASOS:
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": "off"}):
                apagado = banco.ejecutar(caso)
            with mock.patch.dict(os.environ, {"CONVERSACION_IA": "on"}):
                encendido = banco.ejecutar(caso)
            self.assertEqual((apagado.guardado, apagado.etapa, apagado.criterios, apagado.fotos),
                             (encendido.guardado, encendido.etapa, encendido.criterios, encendido.fotos), caso["id"])


class ConversacionDificilTest(unittest.TestCase):
    """Un método por caso. Los casos con hoy_falla quedan como fallo esperado hasta la etapa 2."""

    def revisar(self, caso):
        with self.assertLogs("arenz", level="INFO"):
            resultado = banco.ejecutar(caso)
        observaciones = [p for p in resultado.llamadas
                         if (p.get("text") or {}).get("format", {}).get("name") == "arenz_conversation_observation"]
        self.assertEqual(len(observaciones), 1, "debe haber una sola observación de la IA por mensaje")
        self.assertTrue(banco.precios_y_areas_validos(resultado, caso),
                        f"precio o área que no viene del inventario: {resultado.visible!r}")
        fallas = [descripcion for descripcion, cumple in caso["espera"] if not cumple(resultado, caso)]
        self.assertEqual(fallas, [], f"{caso['id']}: {resultado!r}")


def _crear_prueba(caso):
    def prueba(self):
        self.revisar(caso)
    prueba.__doc__ = f"[{caso['grupo']}] {caso['cliente']!r}" + (f" — hoy: {caso['hoy_falla']}" if caso.get("hoy_falla") else "")
    return unittest.expectedFailure(prueba) if caso.get("hoy_falla") else prueba


for _caso in banco.CASOS:
    setattr(ConversacionDificilTest, f"test_{_caso['id']}", _crear_prueba(_caso))


if __name__ == "__main__":
    unittest.main()
