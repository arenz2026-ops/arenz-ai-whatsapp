"""'Esas fotos no son reales, envíame fotos reales' (18/11, Park Prado) es un pedido de fotos reales, no un rechazo."""
import unittest

import app


class PedidoDeFotosRealesTest(unittest.TestCase):
    def test_pedidos_de_fotos_reales(self):
        for texto in ("Esas fotos no son reales, envíame fotos reales", "¿Son fotos reales?", "¿esas imágenes son renders?",
                      "mándame fotos actuales"):
            self.assertTrue(app.asks_real_photos(texto), texto)
        for texto in ("Envíame más fotos", "Sí", "realmente me gusta"):
            self.assertFalse(app.asks_real_photos(texto), texto)


if __name__ == "__main__":
    unittest.main()
