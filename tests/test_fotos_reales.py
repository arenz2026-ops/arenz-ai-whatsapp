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


    def test_reclamo_de_imagen_equivocada(self):
        for texto in ("Es la foto del nro de cuenta, no es imagen de un inmueble", "Es el nro de cuenta de tu empresa",
                      "esa foto no es del departamento", "la imagen no corresponde", "me mandaste una imagen equivocada",
                      "eso es un voucher"):
            self.assertTrue(app.wrong_image_complaint(texto), texto)
        for texto in ("¿Cuál es el número de cuenta para separar?", "pásame el número de cuenta",
                      "no es un departamento lo que busco, es una casa", "Envíame más fotos", "Sí"):
            self.assertFalse(app.wrong_image_complaint(texto), texto)


if __name__ == "__main__":
    unittest.main()
