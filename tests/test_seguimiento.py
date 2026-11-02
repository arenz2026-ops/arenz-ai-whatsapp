"""Seguimiento automático ARENZ (followups.py): a quién, cuándo y con qué mensaje."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app
import banco_conversaciones as banco
import followups

# 15:00 en Lima: horario comercial.
AHORA = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)


def sesion(etapa="qualified", horas=21, **estado):
    return {"phone": "51911111111", "stage": etapa, "state": estado, "summary": "",
            "updated_at": (AHORA - timedelta(hours=horas)).isoformat()}


class TiendaFalsa:
    def __init__(self, sesiones, previous, escribio=False):
        self.sesiones, self.escribio = sesiones, escribio
        self.memory = mock.Mock()
        self.memory.load_session.return_value = previous
        self.guardados = []

    def due_sessions(self, now, config):
        return [s for s in self.sesiones if not followups.already_followed(s)]

    def claim(self, session, now):
        return not self.escribio

    def record(self, phone, previous, stored, now):
        self.guardados.append(stored)


def con_nombre(previous, nombre):
    previous["state"]["customer_name"] = nombre
    return previous


class MensajeTest(unittest.TestCase):
    def test_tras_un_listado_pregunta_por_las_opciones(self):
        previous = con_nombre(banco.historial("qualified", banco.CRITERIOS_PL, "sí", banco.LISTADO_PL), "César")
        texto, guardado = followups.followup_message(previous)
        self.assertTrue(texto.startswith("Hola César, ¿cómo estás? 😊 Soy Cecilia"))
        self.assertIn("opciones de departamentos que te compartí en Pueblo Libre", texto)
        self.assertIn("Responde con el número de la opción", texto)
        self.assertEqual(texto, guardado)

    def test_tras_una_ficha_ofrece_la_visita_y_guarda_la_referencia(self):
        previous = banco.historial("property_detail", banco.CRITERIOS_PL, "1", banco.FICHA_LIBERTADOR)
        texto, guardado = followups.followup_message(previous)
        self.assertTrue(texto.startswith("Hola, ¿cómo estás? 😊"))
        self.assertIn("¿Quieres que coordinemos una visita al departamento?", texto)
        self.assertIn("Ref. C2-268EAF11F560", guardado)
        self.assertNotIn("C2-", app.customer_visible_reply(texto))

    def test_un_si_al_seguimiento_pide_la_visita(self):
        previous = banco.historial("property_detail", banco.CRITERIOS_PL, "1", banco.FICHA_LIBERTADOR)
        _, guardado = followups.followup_message(previous)
        tras = banco.historial("property_detail", banco.CRITERIOS_PL, "1", banco.FICHA_LIBERTADOR, "", guardado)
        self.assertEqual(app.bot_offer(tras), "visita")

    def test_sin_listado_ni_ficha_no_hay_seguimiento(self):
        self.assertIsNone(followups.followup_message(banco.historial("qualified", {}, "hola", app.INTRODUCTION)))


class EnvioTest(unittest.TestCase):
    def setUp(self):
        self.previous = banco.historial("qualified", banco.CRITERIOS_PL, "sí", banco.LISTADO_PL)

    def test_envia_y_guarda_una_vez(self):
        tienda, enviar = TiendaFalsa([sesion()], self.previous), mock.Mock()
        resultado = followups.run(tienda, now=AHORA, send=enviar)
        self.assertEqual(resultado[0][1], "enviado")
        enviar.assert_called_once()
        self.assertEqual(len(tienda.guardados), 1)

    def test_no_insiste_si_ya_hubo_seguimiento(self):
        ya = sesion(followup_sent_at=(AHORA - timedelta(hours=2)).isoformat())
        enviar = mock.Mock()
        self.assertEqual(followups.run(TiendaFalsa([ya], self.previous), now=AHORA, send=enviar), [])
        enviar.assert_not_called()

    def test_si_el_cliente_escribio_mientras_tanto_no_envia(self):
        enviar = mock.Mock()
        resultado = followups.run(TiendaFalsa([sesion()], self.previous, escribio=True), now=AHORA, send=enviar)
        self.assertEqual(resultado[0][1], "cliente_escribio")
        enviar.assert_not_called()

    def test_fuera_de_horario_no_envia(self):
        noche = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)  # 22:00 en Lima
        enviar = mock.Mock()
        self.assertEqual(followups.run(TiendaFalsa([sesion()], self.previous), now=noche, send=enviar), [])
        enviar.assert_not_called()

    def test_simular_no_envia_ni_marca(self):
        tienda, enviar = TiendaFalsa([sesion()], self.previous), mock.Mock()
        tienda.claim = mock.Mock()
        resultado = followups.run(tienda, now=AHORA, dry_run=True, send=enviar)
        self.assertEqual(resultado[0][1], "simulado")
        enviar.assert_not_called(); tienda.claim.assert_not_called()

    def test_consulta_solo_etapas_y_ventana_validas(self):
        memoria = mock.Mock(url="https://x.supabase.co", headers={})
        respuesta = mock.Mock(); respuesta.json.return_value = []
        with mock.patch.object(followups.requests, "get", return_value=respuesta) as get:
            followups.FollowupStore(memoria).due_sessions(AHORA, followups.settings())
        params = dict((k, v) for k, v in get.call_args.kwargs["params"] if k != "updated_at")
        self.assertEqual(params["stage"], "in.(qualified,property_detail)")
        ventana = [v for k, v in get.call_args.kwargs["params"] if k == "updated_at"]
        self.assertEqual(ventana, [f"lt.{(AHORA - timedelta(hours=20)).isoformat()}",
                                   f"gt.{(AHORA - timedelta(hours=23.5)).isoformat()}"])

    def test_la_marca_exige_que_el_cliente_no_haya_escrito(self):
        memoria = mock.Mock(url="https://x.supabase.co", headers={})
        respuesta = mock.Mock(); respuesta.json.return_value = []
        s = sesion(customer_name="Ana")
        with mock.patch.object(followups.requests, "patch", return_value=respuesta) as patch:
            self.assertFalse(followups.FollowupStore(memoria).claim(s, AHORA))
        self.assertEqual(patch.call_args.kwargs["params"]["updated_at"], f"eq.{s['updated_at']}")
        self.assertEqual(patch.call_args.kwargs["json"]["state"]["customer_name"], "Ana")


class ScriptTest(unittest.TestCase):
    def test_apagado_por_defecto(self):
        import importlib.util
        ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "enviar_seguimientos.py")
        spec = importlib.util.spec_from_file_location("enviar_seguimientos", ruta)
        modulo = importlib.util.module_from_spec(spec); spec.loader.exec_module(modulo)
        with mock.patch.dict(os.environ, {"SEGUIMIENTO_AUTOMATICO": ""}), \
                mock.patch.object(modulo.sys, "argv", ["x"]), mock.patch.object(modulo.followups, "run") as run:
            modulo.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
