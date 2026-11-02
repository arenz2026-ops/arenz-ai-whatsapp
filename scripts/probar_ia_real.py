"""Prueba la clasificación con la API real de OpenAI antes de encender CONVERSACION_IA en producción.

Envía a OpenAI la observación de cada caso del banco (con CONVERSACION_IA=on, inventario simulado) y compara
el tipo de respuesta que devuelve el modelo con el esperado. También muestra los tokens reales por llamada.
Solo hace la llamada de observación: no redacta fichas ni escribe en Supabase ni en WhatsApp.

Uso (cuesta una llamada a gpt-4.1-mini por caso, unos 2.500 tokens cada una):
    OPENAI_API_KEY=sk-... python scripts/probar_ia_real.py
"""
import json
import os
import statistics
import sys
from unittest import mock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [RAIZ, os.path.join(RAIZ, "tests")]
for clave in ("VERIFY_TOKEN", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "APP_SECRET"):
    os.environ.setdefault(clave, "prueba")
os.environ.pop("SUPABASE_URL", None)
os.environ["CONVERSACION_IA"] = "on"

import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import app  # noqa: E402
import banco_conversaciones as banco  # noqa: E402


def main():
    if not app.OPENAI_API_KEY:
        sys.exit("Falta OPENAI_API_KEY.")
    usos, aciertos, filas = [], 0, []
    original = app.response_usage_metadata

    def capturar(cuerpo, inicio):
        datos = original(cuerpo, inicio)
        usos.append(datos)
        return datos

    for caso in banco.CASOS:
        previous = caso["historia"]
        with mock.patch.object(app, "get_inventory_store", return_value=banco.InventarioFalso(caso.get("enviadas", ()))), \
                mock.patch.object(app, "get_conversation_memory_store", return_value=None), \
                mock.patch.object(app, "response_usage_metadata", capturar):
            text = app.resume_answer_as_command(previous, app.normalize_client_text(caso["cliente"]))
            observacion = app.observe_conversation(banco.TELEFONO, text, "FALLBACK", {}, previous, True, raw_text=caso["cliente"])
        esperado = caso["ia"]["respuesta_cliente"]
        obtenido = app.customer_reply(observacion) or {}
        ok = obtenido.get("tipo") == esperado["tipo"] and obtenido.get("pedido", "ninguno") == esperado["pedido"]
        aciertos += ok
        filas.append((caso["id"], caso["cliente"], esperado, obtenido, (observacion or {}).get("assistant_reply")))
        print(f"{'OK ' if ok else 'MAL'} {caso['id']:44} {caso['cliente'][:30]!r:34} esperado={esperado['tipo']}/{esperado['pedido']}"
              f"  obtenido={obtenido.get('tipo')}/{obtenido.get('pedido')}")
    print(f"\nCoinciden {aciertos} de {len(banco.CASOS)} (tipo y pedido).")
    entrada = [u["input_tokens"] for u in usos if u.get("input_tokens") is not None]
    salida = [u["output_tokens"] for u in usos if u.get("output_tokens") is not None]
    if entrada:
        print(f"Tokens reales por mensaje: entrada promedio {statistics.mean(entrada):.0f} (máx. {max(entrada)}), "
              f"salida promedio {statistics.mean(salida):.0f} (máx. {max(salida)}).")
    with open(os.path.join(RAIZ, "probar_ia_real.json"), "w", encoding="utf-8") as archivo:
        json.dump([{"id": f[0], "cliente": f[1], "esperado": f[2], "obtenido": f[3], "texto_ia": f[4]} for f in filas],
                  archivo, ensure_ascii=False, indent=1)
    print("Detalle en probar_ia_real.json")


if __name__ == "__main__":
    main()
