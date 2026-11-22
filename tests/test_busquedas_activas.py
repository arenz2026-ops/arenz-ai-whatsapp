"""Comprobaciones estáticas de las búsquedas activas (la lógica se prueba en SQL: sql/busquedas_activas_pruebas.sql)."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL = (ROOT / "sql" / "busquedas_activas.sql").read_text(encoding="utf-8")
TESTS_SQL = (ROOT / "sql" / "busquedas_activas_pruebas.sql").read_text(encoding="utf-8")
CRM = (ROOT / "crm" / "index.html").read_text(encoding="utf-8")


class MigracionTest(unittest.TestCase):
    def test_nace_apagada(self):
        self.assertRegex(SQL, r"enabled boolean not null default false")

    def test_el_trigger_del_inventario_solo_marca_el_cambio(self):
        self.assertIn("for each statement execute function public.crm_inventory_changed()", SQL)
        body = re.search(r"function public\.crm_inventory_changed\(\).*?\$\$(.*?)\$\$", SQL, re.S).group(1)
        self.assertIn("inventory_changed_at = now()", body)
        self.assertIn("exception when others then null", body)   # nunca frena una carga de inventario
        self.assertNotIn("crm_tasks", body)

    def test_una_coincidencia_una_fila(self):
        self.assertIn("unique (search_id, ref)", SQL)
        self.assertIn("on conflict (search_id, ref) do nothing", SQL)

    def test_excluye_unidades_no_vigentes(self):
        self.assertIn("c.flagged_state = 'active_confirmed' and p.lifecycle_state = 'active_confirmed'", SQL)

    def test_suprime_pausadas_cerradas_y_no_contactar(self):
        for motivo in ("búsqueda pausada", "búsqueda cerrada", "No contactar", "requisito obligatorio pendiente"):
            self.assertIn(motivo, SQL)

    def test_guardas_de_carga_incompleta_y_espera(self):
        self.assertIn("Posible carga incompleta", SQL)
        self.assertIn("esperando que termine la carga de inventario", SQL)

    def test_funciones_del_crm_solo_para_usuarios(self):
        for fn in ("crm_search_get", "crm_search_save", "crm_search_set_status", "crm_search_set_outcome",
                   "crm_search_simulate", "crm_search_activate", "crm_search_deactivate"):
            self.assertRegex(SQL, rf"'{fn}\(")
        self.assertIn("revoke all on function public.%s from public, anon, authenticated", SQL)

    def test_las_pruebas_se_deshacen(self):
        self.assertIn("raise exception 'RESULTADO DE PRUEBAS", TESTS_SQL)
        self.assertIn("S/ 465,000 a S/ 440,000", TESTS_SQL)


class CrmTest(unittest.TestCase):
    def test_ficha_y_panel(self):
        for rpc in ("crm_search_get", "crm_search_save", "crm_search_set_status", "crm_search_reset_from_bot",
                    "crm_search_set_outcome", "crm_search_admin_status", "crm_search_simulate", "crm_search_activate"):
            self.assertIn(f'"{rpc}"', CRM)
        self.assertIn('id="srchSection"', CRM)
        self.assertIn('id="alertAdmin"', CRM)

    def test_activar_pide_confirmacion(self):
        self.assertIn("Confirmar activación", CRM)


if __name__ == "__main__":
    unittest.main()
