"""media_import tests. Photos are generated in memory; nothing leaves this process."""
import contextlib, io, os, tempfile, unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from PIL import Image

import media_import as mi

BASE_URL = "https://db.example"
PID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def photo(size=(320, 240), color="red", fmt="JPEG", mode="RGB", **save):
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt, **save)
    return buffer.getvalue()


def ok(payload=None):
    response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = payload or []
    return response


class CompressionTests(unittest.TestCase):
    def test_a_large_photo_ends_within_1600px_and_1mb(self):
        noisy = Image.frombytes("RGB", (2400, 1800), os.urandom(2400 * 1800 * 3))  # worst case for JPEG
        buffer = io.BytesIO(); noisy.save(buffer, "PNG")
        jpeg, size, original = mi.compress_photo(buffer.getvalue())
        self.assertEqual(original, (2400, 1800))
        self.assertLessEqual(max(size), mi.MAX_SIDE)
        self.assertLessEqual(len(jpeg), mi.MAX_BYTES)
        self.assertEqual(Image.open(io.BytesIO(jpeg)).format, "JPEG")

    def test_phone_rotation_is_applied_and_metadata_with_gps_removed(self):
        exif = Image.Exif(); exif[0x0112] = 6; exif[0x8825] = {2: (12.0, 5.0, 0.0)}  # rotate 90°, GPS latitude
        jpeg, size, _ = mi.compress_photo(photo((200, 100), exif=exif.tobytes()))
        self.assertEqual(size, (100, 200))
        self.assertEqual(dict(Image.open(io.BytesIO(jpeg)).getexif()), {})

    def test_transparency_becomes_white(self):
        jpeg, _, _ = mi.compress_photo(photo((40, 40), (0, 0, 0, 0), "PNG", "RGBA"))
        self.assertEqual(Image.open(io.BytesIO(jpeg)).getpixel((5, 5)), (255, 255, 255))

    def test_a_small_photo_is_not_enlarged(self):
        _, size, _ = mi.compress_photo(photo((300, 200)))
        self.assertEqual(size, (300, 200))

    def test_a_file_that_is_not_an_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no es una imagen"):
            mi.compress_photo(b"no soy una foto")

    def test_object_name_is_readable_and_tied_to_the_content(self):
        first = mi.object_path("ARZ-000001", "Cocina Ñandú (1).JPG", b"a")
        self.assertRegex(first, r"^ARZ-000001/cocina-nandu-1-[0-9a-f]{10}\.jpg$")
        self.assertEqual(first, mi.object_path("ARZ-000001", "Cocina Ñandú (1).JPG", b"a"))
        self.assertNotEqual(first, mi.object_path("ARZ-000001", "Cocina Ñandú (1).JPG", b"b"))
        self.assertEqual(mi.public_url(BASE_URL + "/", "ARZ-1/a b.jpg"),
                         "https://db.example/storage/v1/object/public/inmuebles/ARZ-1/a%20b.jpg")


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, data=b"x"):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_one_group_per_reference_in_natural_order_without_system_files(self):
        for name in ("ARZ-1/10.jpg", "ARZ-1/2.jpg", "ARZ-1/.DS_Store", "ARZ-1/Thumbs.db", "ARZ-2/a.png", ".oculta/x.jpg"):
            self.write(name)
        groups = [(reference, [os.path.basename(path) for path in paths]) for reference, paths in mi.collect(self.root)]
        self.assertEqual(groups, [("ARZ-1", ["2.jpg", "10.jpg"]), ("ARZ-2", ["a.png"])])

    def test_a_reference_folder_can_be_given_directly(self):
        self.write("ARZ-7/1.jpg")
        self.assertEqual([reference for reference, _ in mi.collect(os.path.join(self.root, "ARZ-7"))], ["ARZ-7"])


P1, P2, P3 = (f"0000000{n}-bbbb-cccc-dddd-eeeeeeeeeeee" for n in (1, 2, 3))
TARGETS = {"ARZ-1": {"kind": "unidad", "property_ids": [PID]},
           "LOS BOSQUES": {"kind": "proyecto", "property_ids": [P1, P2, P3]}}
PROJECT = "proyectos/los-bosques"


class PlanTests(unittest.TestCase):
    FILES = {"ARZ-1/1.jpg": photo(color="red"), "ARZ-1/2.jpg": photo(color="blue"), "ARZ-1/3.heic": b"heic",
             "ARZ-1/4.jpg": b"roto", "ARZ-9/1.jpg": photo(), "LOS BOSQUES/fachada.jpg": photo(color="green")}

    def plan(self, groups, existing=None, approve=False):
        return mi.plan_media(groups, TARGETS, existing or {}, BASE_URL, approve, read=self.FILES.__getitem__)

    def url(self, name, folder=None):
        group, filename = name.split("/")
        return mi.public_url(BASE_URL, mi.object_path(folder or group, filename, self.FILES[name]))

    def row(self, name, property_id=PID, approved=False, folder=None):
        return {"property_id": property_id, "media_url": self.url(name, folder), "approved_for_client": approved}

    def test_unit_photos_are_uploaded_unapproved_in_folder_order(self):
        plans = self.plan([("ARZ-1", ["ARZ-1/1.jpg", "ARZ-1/2.jpg"])])
        self.assertEqual([(p["action"], p["display_order"], p["approve"], p["missing"]) for p in plans],
                         [("UPLOAD", 1, False, [PID]), ("UPLOAD", 2, False, [PID])])
        self.assertEqual(plans[0]["url"], self.url("ARZ-1/1.jpg"))

    def test_a_project_photo_is_uploaded_once_for_every_unit(self):
        [plan] = self.plan([("LOS BOSQUES", ["LOS BOSQUES/fachada.jpg"])])
        self.assertEqual((plan["action"], plan["missing"], plan["display_order"]), ("UPLOAD", [P1, P2, P3], 101))
        self.assertRegex(plan["path"], r"^proyectos/los-bosques/fachada-[0-9a-f]{10}\.jpg$")

    def test_new_units_of_a_project_get_its_existing_photos(self):
        existing = {"LOS BOSQUES": [self.row("LOS BOSQUES/fachada.jpg", P1, True, PROJECT),
                                    self.row("LOS BOSQUES/fachada.jpg", P2, True, PROJECT)]}
        [plan] = self.plan([("LOS BOSQUES", ["LOS BOSQUES/fachada.jpg"])], existing)
        self.assertEqual((plan["action"], plan["missing"]), ("UPLOAD", [P3]))

    def test_problems_are_rejected_one_photo_at_a_time(self):
        plans = self.plan([("ARZ-1", ["ARZ-1/3.heic", "ARZ-1/4.jpg"]), ("ARZ-9", ["ARZ-9/1.jpg"])])
        self.assertEqual([p["action"] for p in plans], ["REJECT"] * 3)
        self.assertIn("no soportado", plans[0]["errors"][0])
        self.assertIn("no es una imagen", plans[1]["errors"][0])
        self.assertIn("'ARZ-9' no es una referencia ni un proyecto", plans[2]["errors"][0])

    def test_rerunning_the_same_folder_uploads_nothing(self):
        existing = {"ARZ-1": [self.row("ARZ-1/1.jpg")]}
        self.assertEqual([p["action"] for p in self.plan([("ARZ-1", ["ARZ-1/1.jpg"])], existing)], ["NO-OP"])

    def test_approve_media_approves_what_is_already_uploaded(self):
        existing = {"ARZ-1": [self.row("ARZ-1/1.jpg"), self.row("ARZ-1/2.jpg", approved=True)]}
        plans = self.plan([("ARZ-1", ["ARZ-1/1.jpg", "ARZ-1/2.jpg"])], existing, approve=True)
        self.assertEqual([p["action"] for p in plans], ["APPROVE", "NO-OP"])


class StoreTests(unittest.TestCase):
    def test_reads_every_page(self):
        pages = [ok([{"property_id": "a"}, {"property_id": "b"}]), ok([{"property_id": "c"}, {"property_id": "d"}]),
                 ok([{"property_id": "e"}])]
        with patch.object(mi, "PAGE", 2), patch.object(mi.requests, "get", side_effect=[ok([])] + pages) as get:
            targets = mi.SupabaseMedia(BASE_URL, "k").resolve(["LOS BOSQUES"])
        self.assertEqual(targets["LOS BOSQUES"]["property_ids"], ["a", "b", "c", "d", "e"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["public_description"], "ilike.Proyecto LOS BOSQUES; unidad*")
        self.assertEqual([c.kwargs["params"]["offset"] for c in get.call_args_list[1:]], [0, 2, 4])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.add("ARZ-1", "01-sala.jpg", photo((2000, 1500)))
        self.env = patch.dict(os.environ, {"SUPABASE_URL": BASE_URL, "SUPABASE_KEY": "service-key"}); self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def add(self, folder, name, data):
        os.makedirs(os.path.join(self.tmp.name, folder), exist_ok=True)
        with open(os.path.join(self.tmp.name, folder, name), "wb") as handle:
            handle.write(data)

    def run_main(self, *flags, media=(), post_result=None):
        def get(url, params=None, **kwargs):
            if url.endswith("/inventory_properties") and "public_reference" in params:
                return ok([{"property_id": PID, "public_reference": "ARZ-1"}] if '"ARZ-1"' in params["public_reference"] else [])
            if url.endswith("/inventory_properties"):
                found = params["public_description"].casefold().startswith("ilike.proyecto los bosques;")
                return ok([{"property_id": p} for p in (P1, P2, P3)] if found else [])
            prefix = params["media_url"][len("like."):-1]
            return ok([row for row in media if row["media_url"].startswith(prefix)])
        out = io.StringIO()
        with patch.object(mi.requests, "get", side_effect=get), \
             patch.object(mi.requests, "post", return_value=post_result or ok()) as post, \
             patch.object(mi.requests, "patch", return_value=ok()) as patch_, contextlib.redirect_stdout(out):
            code = mi.main([self.tmp.name, *flags])
        return code, out.getvalue(), post, patch_

    def test_dry_run_writes_nothing(self):
        for flags in ((), ("--approve-media",)):
            code, out, post, patch_ = self.run_main(*flags)
            self.assertEqual(code, 0)
            post.assert_not_called(); patch_.assert_not_called()
            self.assertIn("DRY-RUN", out)
            self.assertIn("2000×1500 → 1600×1200", out)

    def test_apply_uploads_to_the_public_bucket_and_registers_unapproved(self):
        code, out, post, patch_ = self.run_main("--apply")
        self.assertEqual(code, 0)
        upload, insert = post.call_args_list
        self.assertRegex(upload.args[0], r"^https://db\.example/storage/v1/object/inmuebles/ARZ-1/01-sala-[0-9a-f]{10}\.jpg$")
        self.assertEqual(upload.kwargs["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(upload.kwargs["headers"]["x-upsert"], "true")
        self.assertLessEqual(len(upload.kwargs["data"]), mi.MAX_BYTES)
        [row] = insert.kwargs["json"]
        self.assertEqual((row["property_id"], row["media_type"], row["approved_for_client"], row["display_order"]), (PID, "photo", False, 1))
        self.assertTrue(row["media_url"].startswith("https://db.example/storage/v1/object/public/inmuebles/ARZ-1/"))
        self.assertNotIn("approved_at", row)
        patch_.assert_not_called()
        self.assertIn("--approve-media", out)

    def test_a_project_folder_uploads_once_and_registers_every_unit(self):
        self.tmp.cleanup(); self.tmp = tempfile.TemporaryDirectory()
        self.add("LOS BOSQUES", "fachada.jpg", photo())
        code, out, post, _ = self.run_main("--apply")
        self.assertEqual(code, 0)
        upload, insert = post.call_args_list
        self.assertIn("/storage/v1/object/inmuebles/proyectos/los-bosques/fachada-", upload.args[0])
        self.assertEqual([(r["property_id"], r["display_order"]) for r in insert.kwargs["json"]], [(P1, 101), (P2, 101), (P3, 101)])
        self.assertIn("LOS BOSQUES (proyecto, 3 unidades)", out)
        self.assertIn("1 fotos subidas (3 registros)", out)

    def test_apply_with_approve_media_approves_new_and_existing_photos(self):
        code, _, post, _ = self.run_main("--apply", "--approve-media")
        self.assertEqual(code, 0)
        [row] = post.call_args_list[1].kwargs["json"]
        self.assertTrue(row["approved_for_client"]); self.assertIn("approved_at", row)
        uploaded = row["media_url"]
        code, _, post, patch_ = self.run_main("--apply", "--approve-media",
                                              media=[{"property_id": PID, "media_url": uploaded, "approved_for_client": False}])
        post.assert_not_called()
        self.assertEqual(patch_.call_args.kwargs["params"], {"media_url": f"eq.{uploaded}", "approved_for_client": "is.false"})
        self.assertTrue(patch_.call_args.kwargs["json"]["approved_for_client"])

    def test_apply_is_refused_when_a_photo_is_rejected(self):
        self.add("ARZ-1", "02.heic", b"heic")
        code, out, post, _ = self.run_main("--apply")
        self.assertEqual(code, 1)
        post.assert_not_called()
        self.assertIn("rechazadas", out)

    def test_a_storage_error_stops_with_a_clear_message(self):
        failing = Mock(status_code=404, text='{"error":"Bucket not found"}')
        failing.raise_for_status.side_effect = mi.requests.HTTPError(response=failing)
        code, out, _, _ = self.run_main("--apply", post_result=failing)
        self.assertEqual(code, 1)
        self.assertIn("Bucket not found", out); self.assertIn("inmuebles", out)


if __name__ == "__main__":
    unittest.main()
