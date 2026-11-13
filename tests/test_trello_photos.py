import io
import unittest
from unittest import mock

from PIL import Image

import app
import trello_photos as tp


def image_bytes(fmt="PNG", size=(3000, 2000)):
    out = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", size, (200, 10, 10, 255) if fmt == "PNG" else (200, 10, 10)).save(out, fmt)
    return out.getvalue()


def att(i, name=None, mime="image/jpeg", upload=True):
    return {"id": f"a{i}", "name": name or f"foto{i}.jpg", "url": f"https://trello.com/dl/{i}", "mimeType": mime, "isUpload": upload, "bytes": 1000}


class HelpersTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(tp.slugify("03_Casa Herrera_P.Libre"), "casa-herrera-p-libre")
        self.assertEqual(tp.slugify("37_Las Praderas_Chorrillos_Construcción"), "las-praderas-chorrillos")
        self.assertEqual(tp.slugify("V017_SURCO SS04"), "v017-surco-ss04")

    def test_client_photo_filter(self):
        self.assertTrue(tp.is_client_photo(att(1)))
        self.assertTrue(tp.is_client_photo(att(2, "IMG_0001.HEIC", mime="")))
        self.assertFalse(tp.is_client_photo(att(3, "brochure.pdf", mime="application/pdf")))
        self.assertFalse(tp.is_client_photo(att(4, upload=False)))
        self.assertFalse(tp.is_client_photo(att(5, "DNI propietario.jpg")))
        self.assertFalse(tp.is_client_photo(att(6, "partida registral.png", mime="image/png")))

    def test_to_jpeg_resizes_and_flattens(self):
        jpeg = tp.to_jpeg(image_bytes("PNG"))
        with Image.open(io.BytesIO(jpeg)) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(max(img.size), tp.MAX_SIDE)

    def test_to_jpeg_reads_heic(self):
        from pillow_heif import register_heif_opener
        register_heif_opener()
        out = io.BytesIO()
        Image.new("RGB", (64, 48), (0, 120, 0)).save(out, "HEIF")
        with Image.open(io.BytesIO(tp.to_jpeg(out.getvalue()))) as img:
            self.assertEqual((img.format, img.size), ("JPEG", (64, 48)))


class RunTest(unittest.TestCase):
    def make(self, attachments, have=None, slugs=None):
        trello = mock.Mock()
        trello.attachments.return_value = attachments
        trello.download.return_value = image_bytes("JPEG", (100, 80))
        store = mock.Mock()
        store.existing_media.return_value = (have or {}, slugs or {})
        store.inventory_cards.return_value = [{"short_link": "abc123", "name": "46_Torre Bolivar_Pueblo Libre_Lanzamiento", "list_name": "PROYECTOS EN LANZAMIENTO"}]
        store.upload.side_effect = lambda path, jpeg: f"https://s/{path}"
        store.sync_units.return_value = 316
        return trello, store

    def test_loads_missing_photos_and_syncs_units(self):
        trello, store = self.make([att(1), att(2, "plano.pdf", mime="application/pdf"), att(3)])
        summary = tp.run(trello, store)
        self.assertEqual(summary, {"tarjetas": 1, "fotos_nuevas": 2, "errores": 0, "unidades_con_fotos": 316})
        rows = [c.args[0] for c in store.add_media.call_args_list]
        self.assertEqual([r["display_order"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["storage_path"], "proyectos/torre-bolivar-pueblo-libre/torre-bolivar-pueblo-libre_trello_01.jpg")
        self.assertTrue(all(r["approved_for_client"] and r["category"] == "foto" and r["trello_short_link"] == "abc123" for r in rows))

    def test_idempotent_skips_loaded_orders(self):
        trello, store = self.make([att(1), att(2)], have={"abc123": {1, 2}})
        summary = tp.run(trello, store)
        self.assertEqual(summary["fotos_nuevas"], 0)
        trello.download.assert_not_called()
        store.sync_units.assert_not_called()

    def test_card_with_curated_photos_is_left_alone(self):
        trello, store = self.make([att(1)], have={"abc123": None})
        tp.run(trello, store)
        trello.attachments.assert_not_called()

    def test_existing_media_marks_curated_cards(self):
        store = tp.MediaStore("https://x.supabase.co", "k", mock.Mock())
        store._get = mock.Mock(return_value=[
            {"trello_short_link": "A", "project_slug": "a", "display_order": 1, "storage_path": "proyectos/a/a_render_01.jpg"},
            {"trello_short_link": "B", "project_slug": "b", "display_order": 1, "storage_path": "proyectos/b/b_trello_01.jpg"},
            {"trello_short_link": "B", "project_slug": "b", "display_order": 2, "storage_path": "proyectos/b/b_trello_02.jpg"}])
        orders, slugs = store.existing_media()
        self.assertEqual(orders, {"A": None, "B": {1, 2}})
        self.assertEqual(slugs, {"a": "A", "b": "B"})

    def test_full_card_not_even_listed(self):
        trello, store = self.make([att(1)], have={"abc123": set(range(1, 16))})
        tp.run(trello, store)
        trello.attachments.assert_not_called()

    def test_slug_taken_by_other_card_gets_suffix(self):
        trello, store = self.make([att(1)], slugs={"torre-bolivar-pueblo-libre": "zzz999"})
        tp.run(trello, store)
        self.assertIn("torre-bolivar-pueblo-libre-abc123", store.add_media.call_args.args[0]["project_slug"])

    def test_broken_photo_does_not_stop_others(self):
        trello, store = self.make([att(1), att(2)])
        trello.download.side_effect = [b"not an image", image_bytes("JPEG", (10, 10))]
        summary = tp.run(trello, store)
        self.assertEqual((summary["fotos_nuevas"], summary["errores"]), (1, 1))

    def test_download_uses_oauth_header(self):
        session = mock.Mock()
        session.get.return_value.content = b"x"
        tp.TrelloClient("K", "T", session).download("https://trello.com/dl/1")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], 'OAuth oauth_consumer_key="K", oauth_token="T"')


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_requires_token(self):
        with mock.patch.dict("os.environ", {"CRON_TOKEN": "secreto"}):
            self.assertEqual(self.client.post("/tareas/fotos-trello").status_code, 403)

    def test_missing_trello_keys(self):
        with mock.patch.dict("os.environ", {"CRON_TOKEN": "s", "TRELLO_KEY": "", "TRELLO_TOKEN": ""}):
            r = self.client.post("/tareas/fotos-trello", headers={"X-Cron-Token": "s"})
        self.assertEqual((r.status_code, r.json["status"]), (503, "no_trello_keys"))

    def test_starts_in_background(self):
        env = {"CRON_TOKEN": "s", "TRELLO_KEY": "k", "TRELLO_TOKEN": "t", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "k"}
        with mock.patch.dict("os.environ", env), mock.patch.object(tp, "start_background", return_value=True) as start:
            r = self.client.post("/tareas/fotos-trello", headers={"X-Cron-Token": "s"})
        self.assertEqual((r.status_code, r.json["status"]), (202, "started"))
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
