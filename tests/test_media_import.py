"""Photo importer tests. Images are generated in memory; nothing leaves this process."""
import contextlib, io, os, random, tempfile, unittest
from unittest.mock import Mock, patch

from PIL import Image

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import media_import as mi

URL = "https://db.example"
PROPERTY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def image_bytes(size=(400, 300), mode="RGB", fmt="PNG", noise=False, exif=None):
    image = Image.new(mode, size, (200, 120, 40, 128) if mode == "RGBA" else (200, 120, 40))
    if noise:
        rng = random.Random(7)
        image = Image.frombytes("RGB", size, bytes(rng.getrandbits(8) for _ in range(size[0] * size[1] * 3)))
    buffer = io.BytesIO()
    image.save(buffer, fmt, **({"exif": exif} if exif is not None else {}))
    return buffer.getvalue()


def ok(payload=None):
    response = Mock(status_code=200, text="")
    response.raise_for_status.return_value = None
    response.json.return_value = payload if payload is not None else []
    return response


class CompressionTests(unittest.TestCase):
    def test_large_photo_becomes_a_jpeg_within_1600px(self):
        data, size = mi.compress_image(image_bytes((4000, 3000)))
        self.assertTrue(data.startswith(b"\xff\xd8")); self.assertEqual(size, (1600, 1200))
        self.assertLessEqual(len(data), mi.MAX_BYTES)

    def test_detailed_photo_is_squeezed_under_1mb(self):
        source = image_bytes((1600, 1600), noise=True)
        self.assertGreater(len(source), mi.MAX_BYTES)
        data, size = mi.compress_image(source)
        self.assertLessEqual(len(data), mi.MAX_BYTES); self.assertLessEqual(max(size), mi.MAX_SIDE)

    def test_transparency_is_flattened_on_white(self):
        data, _ = mi.compress_image(image_bytes(mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "RGB")

    def test_orientation_is_applied_and_metadata_dropped(self):
        exif = Image.Exif(); exif[0x0112] = 6; exif[0x010F] = "Phone"  # rotate 90°, camera maker
        data, size = mi.compress_image(image_bytes((400, 200), fmt="JPEG", exif=exif.tobytes()))
        self.assertEqual(size, (200, 400))
        self.assertEqual(len(Image.open(io.BytesIO(data)).getexif()), 0)

    def test_unreadable_file_is_rejected(self):
        with self.assertRaises(ValueError):
            mi.compress_image(b"not an image")


class FolderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, *parts, data=None):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(image_bytes() if data is None else data)
        return path


class FolderTests(FolderCase):
    def test_one_subfolder_per_reference_in_name_order(self):
        self.put("c2-abc123", "02-sala.png"); self.put("c2-abc123", "01-fachada.jpg")
        self.put("c2-abc123", "notas.txt", data=b"x"); self.put("c2-abc123", ".DS_Store", data=b"x")
        self.put("C2-DEF456", "a.webp")
        groups = mi.scan_folder(self.root)
        self.assertEqual([g[0] for g in groups], ["C2-ABC123", "C2-DEF456"])
        self.assertEqual([os.path.basename(p) for p in groups[0][1]], ["01-fachada.jpg", "02-sala.png"])
        self.assertEqual(groups[0][2], ["notas.txt"])

    def test_a_folder_of_photos_is_one_reference(self):
        self.put("C2-ABC123", "1.jpg")
        groups = mi.scan_folder(os.path.join(self.root, "C2-ABC123"))
        self.assertEqual([(g[0], len(g[1])) for g in groups], [("C2-ABC123", 1)])

    def test_object_path_is_stable_and_url_safe(self):
        path = mi.object_path("C2-ABC123", "Fachada Ñuñoa (1).JPG", b"same")
        self.assertEqual(path, mi.object_path("C2-ABC123", "Fachada Ñuñoa (1).JPG", b"same"))
        self.assertRegex(path, r"^C2-ABC123/fachada-nunoa-1-[0-9a-f]{10}\.jpg$")
        self.assertNotEqual(path, mi.object_path("C2-ABC123", "Fachada Ñuñoa (1).JPG", b"other"))
        self.assertEqual(mi.public_url(URL + "/", "inmuebles", path), f"{URL}/storage/v1/object/public/inmuebles/{path}")


class PlanAndApplyTests(FolderCase):
    def plans(self, existing=None, approve=False, ids=None):
        groups = mi.scan_folder(self.root)
        ids = {"C2-ABC123": PROPERTY_ID} if ids is None else ids
        return mi.plan_folder(groups, ids, existing or {}, URL, approve=approve)

    def test_unknown_reference_is_an_error(self):
        self.put("C2-NOPE", "1.jpg")
        plans = self.plans(ids={})
        self.assertIn("no existe esa referencia en el inventario", plans[0]["errors"])

    def test_actions_new_existing_and_approve(self):
        self.put("C2-ABC123", "1.jpg"); self.put("C2-ABC123", "2.jpg", data=image_bytes((500, 300)))
        first = self.plans()[0]["photos"]
        self.assertEqual([p["action"] for p in first], ["NUEVA", "NUEVA"])
        existing = {(PROPERTY_ID, first[0]["url"]): {"media_id": "m1", "approved_for_client": False},
                    (PROPERTY_ID, first[1]["url"]): {"media_id": "m2", "approved_for_client": True}}
        self.assertEqual([p["action"] for p in self.plans(existing)[0]["photos"]], ["IGUAL", "IGUAL"])
        again = self.plans(existing, approve=True)[0]["photos"]
        self.assertEqual([(p["action"], p.get("media_id")) for p in again], [("APROBAR", "m1"), ("IGUAL", None)])

    def test_apply_uploads_then_registers_unapproved(self):
        self.put("C2-ABC123", "01.jpg"); self.put("C2-ABC123", "02.jpg", data=image_bytes((500, 300)))
        store = mi.SupabaseMedia(URL, "k")
        with patch.object(mi.requests, "post", return_value=ok()) as post, patch.object(mi.requests, "patch") as patch_:
            totals = mi.apply_plans(store, self.plans())
        uploads, insert = post.call_args_list[:2], post.call_args_list[2]
        self.assertTrue(uploads[0].args[0].startswith(f"{URL}/storage/v1/object/inmuebles/C2-ABC123/01-"))
        self.assertEqual(uploads[0].kwargs["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(uploads[0].kwargs["headers"]["x-upsert"], "true")
        rows = insert.kwargs["json"]
        self.assertEqual([(r["display_order"], r["media_type"], r["approved_for_client"], r["approved_at"]) for r in rows],
                         [(1, "photo", False, None), (2, "photo", False, None)])
        self.assertTrue(rows[0]["media_url"].startswith(f"{URL}/storage/v1/object/public/inmuebles/C2-ABC123/"))
        patch_.assert_not_called()
        self.assertEqual(totals, {"subidas": 2, "aprobadas": 0})

    def test_approve_media_marks_new_and_existing_rows(self):
        self.put("C2-ABC123", "01.jpg"); self.put("C2-ABC123", "02.jpg", data=image_bytes((500, 300)))
        url = self.plans()[0]["photos"][0]["url"]
        existing = {(PROPERTY_ID, url): {"media_id": "m1", "approved_for_client": False}}
        with patch.object(mi.requests, "post", return_value=ok()) as post, patch.object(mi.requests, "patch", return_value=ok()) as patch_:
            totals = mi.apply_plans(mi.SupabaseMedia(URL, "k"), self.plans(existing, approve=True), approve=True)
        rows = post.call_args_list[-1].kwargs["json"]
        self.assertEqual(len(rows), 1); self.assertTrue(rows[0]["approved_for_client"]); self.assertIsNotNone(rows[0]["approved_at"])
        self.assertEqual(patch_.call_args.kwargs["params"], {"media_id": "in.(m1)"})
        self.assertTrue(patch_.call_args.kwargs["json"]["approved_for_client"])
        self.assertEqual(totals, {"subidas": 1, "aprobadas": 2})

    def test_failed_upload_stops_before_registering(self):
        self.put("C2-ABC123", "01.jpg")
        rejected = Mock(status_code=404, text="Bucket not found")
        with patch.object(mi.requests, "post", return_value=rejected) as post, self.assertRaises(RuntimeError):
            mi.apply_plans(mi.SupabaseMedia(URL, "k"), self.plans())
        self.assertEqual(post.call_count, 1)


class CommandTests(FolderCase):
    def run_main(self, *args, env=None):
        out = io.StringIO()
        with patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out):
            if env is None:
                os.environ.pop("SUPABASE_URL", None); os.environ.pop("SUPABASE_KEY", None)
            code = mi.main([self.root, *args])
        return code, out.getvalue()

    def test_dry_run_writes_nothing(self):
        self.put("C2-ABC123", "01.jpg")
        lookups = [ok([{"property_id": PROPERTY_ID, "public_reference": "C2-ABC123"}]), ok([])]
        with patch.object(mi.requests, "get", side_effect=lookups), patch.object(mi.requests, "post") as post, \
             patch.object(mi.requests, "patch") as patch_, patch.object(mi.requests, "put") as put:
            code, out = self.run_main("--approve-media", env={"SUPABASE_URL": URL, "SUPABASE_KEY": "k"})
        self.assertEqual(code, 0)
        post.assert_not_called(); patch_.assert_not_called(); put.assert_not_called()
        self.assertIn("[NUEVA  ]  1. 01.jpg", out); self.assertIn("DRY-RUN", out)

    def test_dry_run_works_without_credentials(self):
        self.put("C2-ABC123", "01.jpg")
        with patch.object(mi.requests, "get") as get, patch.object(mi.requests, "post") as post:
            code, out = self.run_main()
        get.assert_not_called(); post.assert_not_called()
        self.assertEqual(code, 0); self.assertIn("no se verificaron", out); self.assertIn("sin aprobar", out)

    def test_apply_needs_credentials(self):
        self.put("C2-ABC123", "01.jpg")
        with self.assertRaises(SystemExit):
            self.run_main("--apply")

    def test_apply_is_refused_while_there_are_errors(self):
        self.put("C2-NOPE", "01.jpg")
        with patch.object(mi.requests, "get", side_effect=[ok([]), ok([])]), patch.object(mi.requests, "post") as post:
            code, out = self.run_main("--apply", env={"SUPABASE_URL": URL, "SUPABASE_KEY": "k"})
        self.assertEqual(code, 1); post.assert_not_called(); self.assertIn("No se subió nada", out)


if __name__ == "__main__":
    unittest.main()
