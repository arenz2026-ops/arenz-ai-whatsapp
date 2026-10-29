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


class PlanTests(unittest.TestCase):
    FILES = {"ARZ-1/1.jpg": photo(color="red"), "ARZ-1/2.jpg": photo(color="blue"),
             "ARZ-1/3.heic": b"heic", "ARZ-1/4.jpg": b"roto", "ARZ-9/1.jpg": photo()}

    def plan(self, groups, existing=None, approve=False):
        return mi.plan_media(groups, {"ARZ-1": PID}, existing or {}, BASE_URL, approve, read=self.FILES.__getitem__)

    def url(self, name):
        return mi.public_url(BASE_URL, mi.object_path("ARZ-1", name.split("/")[1], self.FILES[name]))

    def test_new_photos_are_uploaded_unapproved_after_the_existing_order(self):
        existing = {PID: [{"media_url": "https://otra/0.jpg", "approved_for_client": True, "display_order": 4}]}
        plans = self.plan([("ARZ-1", ["ARZ-1/1.jpg", "ARZ-1/2.jpg"])], existing)
        self.assertEqual([(p["action"], p["display_order"], p["approve"]) for p in plans], [("UPLOAD", 5, False), ("UPLOAD", 6, False)])
        self.assertEqual(plans[0]["url"], self.url("ARZ-1/1.jpg"))

    def test_problems_are_rejected_one_photo_at_a_time(self):
        plans = self.plan([("ARZ-1", ["ARZ-1/3.heic", "ARZ-1/4.jpg"]), ("ARZ-9", ["ARZ-9/1.jpg"])])
        self.assertEqual([p["action"] for p in plans], ["REJECT"] * 3)
        self.assertIn("no soportado", plans[0]["errors"][0])
        self.assertIn("no es una imagen", plans[1]["errors"][0])
        self.assertIn("ARZ-9", plans[2]["errors"][0])

    def test_rerunning_the_same_folder_uploads_nothing(self):
        existing = {PID: [{"media_url": self.url("ARZ-1/1.jpg"), "approved_for_client": False, "display_order": 1}]}
        self.assertEqual([p["action"] for p in self.plan([("ARZ-1", ["ARZ-1/1.jpg"])], existing)], ["NO-OP"])

    def test_approve_media_approves_what_is_already_uploaded(self):
        existing = {PID: [{"media_url": self.url("ARZ-1/1.jpg"), "approved_for_client": False, "display_order": 1},
                          {"media_url": self.url("ARZ-1/2.jpg"), "approved_for_client": True, "display_order": 2}]}
        plans = self.plan([("ARZ-1", ["ARZ-1/1.jpg", "ARZ-1/2.jpg"])], existing, approve=True)
        self.assertEqual([p["action"] for p in plans], ["APPROVE", "NO-OP"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "ARZ-1"))
        with open(os.path.join(self.tmp.name, "ARZ-1", "01-sala.jpg"), "wb") as handle:
            handle.write(photo((2000, 1500)))
        self.env = patch.dict(os.environ, {"SUPABASE_URL": BASE_URL, "SUPABASE_KEY": "service-key"}); self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def run_main(self, *flags, media=None):
        def get(url, **kwargs):
            if url.endswith("/inventory_properties"):
                return ok([{"property_id": PID, "public_reference": "ARZ-1"}])
            return ok(media or [])
        out = io.StringIO()
        with patch.object(mi.requests, "get", side_effect=get), \
             patch.object(mi.requests, "post", return_value=ok()) as post, \
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
        row = insert.kwargs["json"]
        self.assertEqual((row["property_id"], row["media_type"], row["approved_for_client"], row["display_order"]), (PID, "photo", False, 1))
        self.assertTrue(row["media_url"].startswith("https://db.example/storage/v1/object/public/inmuebles/ARZ-1/"))
        self.assertNotIn("approved_at", row)
        patch_.assert_not_called()
        self.assertIn("--approve-media", out)

    def test_apply_with_approve_media_approves_new_and_existing_photos(self):
        code, _, post, _ = self.run_main("--apply", "--approve-media")
        self.assertEqual(code, 0)
        row = post.call_args_list[1].kwargs["json"]
        self.assertTrue(row["approved_for_client"]); self.assertIn("approved_at", row)
        uploaded_url = row["media_url"]
        code, _, post, patch_ = self.run_main("--apply", "--approve-media",
                                              media=[{"property_id": PID, "media_url": uploaded_url, "approved_for_client": False, "display_order": 1}])
        post.assert_not_called()
        self.assertEqual(patch_.call_args.kwargs["params"], {"property_id": f"eq.{PID}", "media_url": f"eq.{uploaded_url}"})
        self.assertTrue(patch_.call_args.kwargs["json"]["approved_for_client"])

    def test_apply_is_refused_when_a_photo_is_rejected(self):
        with open(os.path.join(self.tmp.name, "ARZ-1", "02.heic"), "wb") as handle:
            handle.write(b"heic")
        code, out, post, _ = self.run_main("--apply")
        self.assertEqual(code, 1)
        post.assert_not_called()
        self.assertIn("rechazadas", out)

    def test_a_storage_error_stops_with_a_clear_message(self):
        failing = Mock(status_code=404, text='{"error":"Bucket not found"}')
        failing.raise_for_status.side_effect = mi.requests.HTTPError(response=failing)
        out = io.StringIO()
        with patch.object(mi.requests, "get", side_effect=lambda url, **k: ok([{"property_id": PID, "public_reference": "ARZ-1"}])
                          if url.endswith("/inventory_properties") else ok()), \
             patch.object(mi.requests, "post", return_value=failing), contextlib.redirect_stdout(out):
            code = mi.main([self.tmp.name, "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Bucket not found", out.getvalue()); self.assertIn("inmuebles", out.getvalue())


if __name__ == "__main__":
    unittest.main()
