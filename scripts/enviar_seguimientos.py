"""Envía el seguimiento automático ARENZ (ver followups.py). Pensado para ejecutarse cada hora.

Uso:
    python scripts/enviar_seguimientos.py            # envía (requiere SEGUIMIENTO_AUTOMATICO=on)
    python scripts/enviar_seguimientos.py --simular  # solo muestra qué enviaría; no envía ni escribe
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import followups  # noqa: E402


def main():
    simular = "--simular" in sys.argv
    if not simular and os.getenv("SEGUIMIENTO_AUTOMATICO", "off").strip().lower() != "on":
        print("SEGUIMIENTO_AUTOMATICO no está en 'on': no se envía nada.")
        return
    memory = app.get_conversation_memory_store()
    if not memory:
        sys.exit("Faltan SUPABASE_URL y SUPABASE_KEY.")
    resultados = followups.run(followups.FollowupStore(memory), dry_run=simular)
    if not resultados:
        print("Nada que enviar (fuera de horario o sin conversaciones pendientes).")
    for telefono, resultado, texto in resultados:
        print(f"{resultado:16} ...{telefono[-4:]}" + (f"\n{texto}\n" if simular and texto else ""))


if __name__ == "__main__":
    main()
