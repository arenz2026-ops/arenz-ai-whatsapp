"""Una unidad de entrega inmediata recibe fotos reales; los renders quedan solo para proyectos en construcción.

18/11, Park Prado (entregado): el cliente recibió imágenes que no eran reales. sync_property_media_from_projects
copiaba a cada unidad todas las imágenes del proyecto sin mirar su categoría (foto, render, piloto, plano).
"""
import unittest
from unittest import mock

import app

MEDIA = [{"property_id": "p1", "media_type": "photo", "media_url": f"https://x/{c}.jpg", "approved_for_client": True,
          "display_order": i, "category": c} for i, c in enumerate(["render", "foto", "piloto", "render", "plano", None], 1)]


def unidad(entrega):
    return {"property_id": "p1", "public_reference": "C2-X", "public_description": f"Entrega según fuente: {entrega}.",
            "source_name": "c2"}


class FotosPorEntregaTest(unittest.TestCase):
    def fotos(self, entrega):
        store = app.InventoryStore("https://x.supabase.co", "k")
        with mock.patch.object(store, "unit_row", return_value=unidad(entrega)), \
                mock.patch.object(store, "_approved_media", return_value={"p1": MEDIA}):
            return store.photos_for_reference("C2-X", 15)

    def test_entrega_inmediata_sin_renders(self):
        self.assertEqual(self.fotos("Entregado"), ["https://x/foto.jpg", "https://x/piloto.jpg", "https://x/plano.jpg",
                                                   "https://x/None.jpg"])

    def test_en_construccion_conserva_renders(self):
        self.assertEqual(len(self.fotos("Dic 2035")), 6)

    def test_pide_la_categoria_a_supabase(self):
        store = app.InventoryStore("https://x.supabase.co", "k")
        with mock.patch.object(app.requests, "get") as get:
            get.return_value.json.return_value = []
            store._approved_media(["p1"])
        self.assertIn("category", get.call_args.kwargs["params"]["select"])

    def test_pedidos_de_fotos_reales(self):
        for texto in ("Esas fotos no son reales, envíame fotos reales", "¿Son fotos reales?", "¿esas imágenes son renders?",
                      "mándame fotos actuales"):
            self.assertTrue(app.asks_real_photos(texto), texto)
        for texto in ("Envíame más fotos", "Sí", "realmente me gusta"):
            self.assertFalse(app.asks_real_photos(texto), texto)


if __name__ == "__main__":
    unittest.main()
