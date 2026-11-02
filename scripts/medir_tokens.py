"""Mide los tokens que se envían a OpenAI por mensaje, usando el banco de conversaciones difíciles.

Uso:
    python scripts/medir_tokens.py                 # CONVERSACION_IA según el entorno (por defecto off)
    python scripts/medir_tokens.py --modo on       # forzar on u off
    python scripts/medir_tokens.py --json out.json # además guarda los textos enviados (para contarlos con otra herramienta)

Cuenta con tiktoken (o200k_base, el tokenizador de gpt-4.1-mini) si está disponible; si no, estima con
4 caracteres por token y lo indica. Para la llamada de observación se cuentan las instrucciones, el
mensaje y el esquema JSON (OpenAI lo incluye en la entrada al usar salidas estructuradas). La salida
de la observación se estima con el JSON que devolvió el modelo en cada caso.
"""
import argparse
import json
import os
import statistics
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [RAIZ, os.path.join(RAIZ, "tests")]
for clave in ("VERIFY_TOKEN", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "APP_SECRET"):
    os.environ.setdefault(clave, "medicion")
os.environ.pop("SUPABASE_URL", None)

import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import banco_conversaciones as banco  # noqa: E402


def contador():
    try:
        import tiktoken
        codificador = tiktoken.get_encoding("o200k_base")
        return (lambda texto: len(codificador.encode(texto))), "tiktoken o200k_base"
    except Exception:  # sin tiktoken o sin acceso a su archivo de vocabulario
        return (lambda texto: round(len(texto) / 4)), "estimación (4 caracteres por token)"


def textos_de_llamada(payload, caso):
    formato = (payload.get("text") or {}).get("format") or {}
    if formato.get("name") == "arenz_conversation_observation":
        entrada = payload["instructions"] + "\n" + payload["input"] + "\n" + json.dumps(formato["schema"], ensure_ascii=False)
        salida = dict(caso["ia"])
        if os.getenv("CONVERSACION_IA", "off") != "on":
            salida.pop("respuesta_cliente", None)  # en off el esquema no tiene ese campo
        return "observacion", entrada, json.dumps(salida, ensure_ascii=False)
    tipo = "ficha" if "Redacta la ficha" in payload["instructions"] else "pregunta_unidad"
    return tipo, payload["instructions"] + "\n" + payload["input"], None


def medir():
    registros = []
    for caso in banco.CASOS:
        resultado = banco.ejecutar(caso)
        llamadas = []
        for payload in resultado.llamadas:
            tipo, entrada, salida = textos_de_llamada(payload, caso)
            llamadas.append({"tipo": tipo, "entrada": entrada, "salida": salida})
        registros.append({"id": caso["id"], "llamadas": llamadas})
    return registros


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="guardar los textos enviados a OpenAI en este archivo")
    parser.add_argument("--modo", choices=("on", "off"), help="valor de CONVERSACION_IA para la medición")
    args = parser.parse_args()
    if args.modo:
        os.environ["CONVERSACION_IA"] = args.modo
    registros = medir()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as archivo:
            json.dump(registros, archivo, ensure_ascii=False, indent=1)
    contar, metodo = contador()
    print(f"Conteo: {metodo} · CONVERSACION_IA={os.getenv('CONVERSACION_IA', 'off')}\n")
    print(f"{'caso':48} {'llamadas':>8} {'obs.entrada':>11} {'obs.salida':>10} {'otras.entrada':>13}")
    obs_entrada, obs_salida, otras, por_mensaje, llamadas_por_mensaje = [], [], [], [], []
    for registro in registros:
        obs = [l for l in registro["llamadas"] if l["tipo"] == "observacion"]
        resto = [l for l in registro["llamadas"] if l["tipo"] != "observacion"]
        entrada = sum(contar(l["entrada"]) for l in obs)
        salida = sum(contar(l["salida"]) for l in obs)
        extra = sum(contar(l["entrada"]) for l in resto)
        obs_entrada.append(entrada); obs_salida.append(salida); otras.append(extra)
        por_mensaje.append(entrada + salida + extra); llamadas_por_mensaje.append(len(registro["llamadas"]))
        print(f"{registro['id']:48} {len(registro['llamadas']):>8} {entrada:>11} {salida:>10} {extra:>13}")
    con_extra = sum(1 for n in llamadas_por_mensaje if n > 1)
    print(f"\nMensajes: {len(registros)}  ·  con más de una llamada a OpenAI: {con_extra}")
    print(f"Observación, entrada: promedio {statistics.mean(obs_entrada):.0f}, máximo {max(obs_entrada)}")
    print(f"Observación, salida:  promedio {statistics.mean(obs_salida):.0f}, máximo {max(obs_salida)}")
    print(f"Otras llamadas (ficha / pregunta sobre la unidad), entrada: promedio {statistics.mean(otras):.0f}, máximo {max(otras)}")
    print(f"Total por mensaje (sin la salida de otras llamadas): promedio {statistics.mean(por_mensaje):.0f}, máximo {max(por_mensaje)}")


if __name__ == "__main__":
    main()
