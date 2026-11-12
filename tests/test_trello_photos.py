"""Carga de las fotos HEIC de Trello (trello_photos.py) y su tarea /tareas/fotos-trello."""
import io
import os
import unittest
from unittest import mock

os.environ.setdefault("VERIFY_TOKEN", "t"); os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("PHONE_NUMBER_ID", "1"); os.environ.setdefault("APP_SECRET", "s")

import requests  # noqa: E402

import app  # noqa: E402
import trello_photos  # noqa: E402


def heic_bytes(width=3024, height=4032, orientation=None):
    """Un HEIC de verdad, como los que sube un iPhone a Trello."""
    import pillow_heif
    from PIL import Image
    image = Image.new("RGB", (width, height), (200, 120, 40))
    heif = pillow_heif.from_pillow(image)
    out = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        heif.save(out, quality=60, exif=exif.tobytes())
    else:
        heif.save(out, quality=60)
    return out.getvalue()


class ClienteFalso(trello_photos.TrelloPhotos):
    """Trello y Supabase en memoria."""

    def __init__(self, adjuntos, filas, lista="PROYECTOS CONSTRUCCION", nombre="01_Unico Magdalena", archivos=None):
        super().__init__("https://db.test", "clave", "trello-key", "trello-token")
        self._adjuntos, self.filas, self._lista, self._nombre = adjuntos, filas, lista, nombre
        self.archivos = archivos or {}
        self.subidas, self.sincronizaciones = {}, 0

    def attachments(self, short_link):
        return self._adjuntos

    def download(self, attachment):
        contenido = self.archivos.get(attachment["id"])
        if isinstance(contenido, Exception):
            raise contenido
        return contenido

    def card(self, short_link):
        return {"short_link": short_link, "name": self._nombre, "list_name": self._lista}

    def media(self, short_link):
        return [dict(fila) for fila in self.filas]

    def project_cards(self):
        return ["uBAWj2Mc"]

    def upload(self, path, jpeg):
        self.subidas[path] = jpeg
        return f"https://db.test/storage/v1/object/public/inmuebles/{path}"

    def insert_media(self, row):
        self.filas.append(dict(row))

    def set_order(self, storage_path, order):
        for fila in self.filas:
            if fila["storage_path"] == storage_path:
                fila["display_order"] = order

    def sync_units(self):
        self.sincronizaciones += 1
        return 42


def renders_y_planos():
    filas = [{"storage_path": f"proyectos/unico-magdalena/unico-magdalena_render_{i:02d}.jpg", "project_slug": "unico-magdalena",
              "category": "render", "display_order": i} for i in range(1, 10)]
    filas += [{"storage_path": f"proyectos/unico-magdalena/unico-magdalena_plano_{i:02d}.jpg", "project_slug": "unico-magdalena",
               "category": "plano", "display_order": 9 + i} for i in range(1, 3)]
    return filas


ADJUNTOS = [
    {"id": "64aa00000000000000000001", "name": "IMG_4501.HEIC", "mimeType": "image/heic", "isUpload": True, "url": "u1", "bytes": 2_000_000},
    {"id": "64aa00000000000000000002", "name": "IMG_4502.heic", "mimeType": "", "isUpload": True, "url": "u2", "bytes": 2_000_000},
    {"id": "64aa00000000000000000003", "name": "render 1.jpg", "mimeType": "image/jpeg", "isUpload": True, "url": "u3", "bytes": 500_000},
    {"id": "64aa00000000000000000004", "name": "web.heic", "mimeType": "image/heic", "isUpload": False, "url": "https://x", "bytes": 0},
]


class ConversionTest(unittest.TestCase):
    def test_heic_a_jpeg_para_whatsapp(self):
        from PIL import Image
        jpeg = trello_photos.heic_to_jpeg(heic_bytes())
        imagen = Image.open(io.BytesIO(jpeg))
        self.assertEqual(imagen.format, "JPEG")
        self.assertEqual(imagen.mode, "RGB")
        self.assertLessEqual(max(imagen.size), 1600)
        self.assertLess(len(jpeg), 5 * 1024 * 1024, "WhatsApp admite imágenes de hasta 5 MB")

    def test_respeta_la_orientacion_del_celular(self):
        from PIL import Image
        # Orientación 6: la foto se tomó con el celular de costado; debe quedar derecha y girada una sola vez.
        # En HEIC la orientación viaja en el contenedor y pillow-heif la aplica al abrir (exif_transpose queda de
        # respaldo y no vuelve a girarla).
        imagen = Image.open(io.BytesIO(trello_photos.heic_to_jpeg(heic_bytes(4032, 3024, orientation=6))))
        self.assertGreater(imagen.size[1], imagen.size[0])
        self.assertEqual(imagen.getexif().get(0x0112, 1), 1, "sin marca de orientación pendiente en el JPG")

    def test_archivo_invalido(self):
        with self.assertRaises(ValueError):
            trello_photos.heic_to_jpeg(b"no es una imagen")


class ReglasTest(unittest.TestCase):
    def test_que_es_heic(self):
        self.assertEqual([trello_photos.is_heic(a) for a in ADJUNTOS], [True, True, False, False])

    def test_nombre_y_categoria(self):
        self.assertEqual(trello_photos.slugify("01_Unico Magdalena"), "unico-magdalena")
        self.assertEqual(trello_photos.slugify("Calle Los Girasoles – Surco"), "calle-los-girasoles-surco")
        self.assertEqual(trello_photos.category_for("PROYECTOS CONSTRUCCION"), "piloto")
        self.assertEqual(trello_photos.category_for("DEPARTAMENTOS SEGUNDO USO"), "foto")


class CargaTest(unittest.TestCase):
    def cliente(self, **extra):
        archivos = {"64aa00000000000000000001": heic_bytes(800, 600), "64aa00000000000000000002": heic_bytes(800, 600)}
        archivos.update(extra.pop("archivos", {}))
        return ClienteFalso(ADJUNTOS, renders_y_planos(), archivos=archivos, **extra)

    def test_carga_los_heic_como_piloto_entre_renders_y_planos(self):
        cliente = self.cliente()
        resumen = trello_photos.load_card(cliente, "uBAWj2Mc")
        self.assertEqual((resumen["heic"], len(resumen["cargadas"]), resumen["errores"]), (2, 2, []))
        self.assertEqual(sorted(cliente.subidas), [
            "proyectos/unico-magdalena/unico-magdalena_piloto_heic_0000000001.jpg",
            "proyectos/unico-magdalena/unico-magdalena_piloto_heic_0000000002.jpg"])
        orden = [f["category"] for f in sorted(cliente.filas, key=lambda f: f["display_order"])]
        self.assertEqual(orden, ["render"] * 9 + ["piloto"] * 2 + ["plano"] * 2)
        self.assertEqual(sorted(f["display_order"] for f in cliente.filas), list(range(1, 14)))
        nuevas = [f for f in cliente.filas if f["category"] == "piloto"]
        self.assertTrue(all(f["approved_for_client"] and f["approved_at"] for f in nuevas))
        self.assertTrue(all(f["trello_short_link"] == "uBAWj2Mc" for f in nuevas))

    def test_dos_veces_no_duplica(self):
        cliente = self.cliente()
        trello_photos.load_card(cliente, "uBAWj2Mc")
        subidas = dict(cliente.subidas)
        resumen = trello_photos.load_card(cliente, "uBAWj2Mc")
        self.assertEqual((len(resumen["cargadas"]), resumen["ya_estaban"]), (0, 2))
        self.assertEqual(cliente.subidas, subidas)
        self.assertEqual(len(cliente.filas), 13)

    def test_un_archivo_danado_no_frena_los_demas(self):
        cliente = self.cliente(archivos={"64aa00000000000000000002": b"roto"})
        resumen = trello_photos.load_card(cliente, "uBAWj2Mc")
        self.assertEqual(len(resumen["cargadas"]), 1)
        self.assertEqual(len(resumen["errores"]), 1)
        self.assertIn("IMG_4502.heic", resumen["errores"][0])

    def test_sin_aprobar(self):
        cliente = self.cliente()
        trello_photos.load_card(cliente, "uBAWj2Mc", approve=False)
        nuevas = [f for f in cliente.filas if f["category"] == "piloto"]
        self.assertTrue(all(not f["approved_for_client"] and "approved_at" not in f for f in nuevas))

    def test_tarjeta_nueva_toma_el_nombre_de_trello(self):
        cliente = ClienteFalso(ADJUNTOS[:1], [], lista="DEPARTAMENTOS SEGUNDO USO", nombre="07_Santo Cristo 304",
                               archivos={"64aa00000000000000000001": heic_bytes(800, 600)})
        trello_photos.load_card(cliente, "abcd1234")
        self.assertEqual(list(cliente.subidas), ["proyectos/santo-cristo-304/santo-cristo-304_foto_heic_0000000001.jpg"])

    def test_run_sincroniza_solo_si_cargo_algo(self):
        cliente = self.cliente()
        resultado = trello_photos.run(cliente)
        self.assertEqual((resultado["cargadas"], resultado["fotos_por_unidad_sincronizadas"], cliente.sincronizaciones), (2, 42, 1))
        resultado = trello_photos.run(cliente)
        self.assertEqual((resultado["cargadas"], resultado["ya_estaban"], cliente.sincronizaciones), (0, 2, 1))


class TareaTest(unittest.TestCase):
    def setUp(self):
        self.cliente = app.app.test_client()

    def post(self, token="secreto", tarjeta=None, env=None):
        entorno = {"CRON_TOKEN": "secreto", "SUPABASE_URL": "https://db.test", "SUPABASE_KEY": "k",
                   "TRELLO_KEY": "tk", "TRELLO_TOKEN": "tt", **(env or {})}
        with mock.patch.dict(os.environ, entorno):
            return self.cliente.post("/tareas/fotos-trello" + (f"?tarjeta={tarjeta}" if tarjeta else ""),
                                     headers={"X-Cron-Token": token})

    def test_protegida(self):
        self.assertEqual(self.post(token="otro").status_code, 403)

    def test_sin_clave_de_trello(self):
        respuesta = self.post(env={"TRELLO_KEY": ""})
        self.assertEqual(respuesta.status_code, 503)
        self.assertEqual(respuesta.get_json()["status"], "falta_configuracion")

    def test_tarjeta_invalida(self):
        self.assertEqual(self.post(tarjeta="../../x").status_code, 400)

    def test_una_tarjeta(self):
        with mock.patch.object(trello_photos, "run", return_value={"tarjetas": 1, "heic": 2, "cargadas": 2, "ya_estaban": 0,
                                                                   "fotos_por_unidad_sincronizadas": 42, "detalle": []}) as run:
            respuesta = self.post(tarjeta="uBAWj2Mc")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()["cargadas"], 2)
        self.assertEqual(run.call_args[0][1], ["uBAWj2Mc"])

    def test_trello_caido(self):
        with mock.patch.object(trello_photos, "run", side_effect=requests.ConnectionError):
            self.assertEqual(self.post().status_code, 502)


if __name__ == "__main__":
    unittest.main()
