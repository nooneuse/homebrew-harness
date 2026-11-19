"""Tests for content-addressed media storage, limits, retrieval and export."""

import hashlib
import stat
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.media import (
    MediaError,
    MediaLimits,
    MediaStore,
    ensure_request_body_size,
    request_body_bounds,
    validate_image,
)
from homebrew_harness.tools.media import render_bar_chart


def jpeg_bytes(width=20, height=10):
    """Minimal JPEG header stream that a real validator accepts."""
    sof = (
        bytes([0xFF, 0xC0, 0x00, 0x0B, 0x08])
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes([0x01, 0x01, 0x11, 0x00])
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def media_code(error):
    return getattr(error, "media_code", None)


def limit_name(error):
    details = getattr(error, "details", {})
    return details.get("limit_name") if isinstance(details, dict) else None


class MediaStoreTestCase(unittest.TestCase):
    """Shared workspace, one real 160x120 PNG chart fixture."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = MediaStore(self.root)
        self.png = render_bar_chart(["alpha", "beta"], [1, 2], width=160, height=120)
        self.sha = hashlib.sha256(self.png).hexdigest()
        self.media_dir = self.root / ".harness" / "media"

    def _write_file(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class FreezeTests(MediaStoreTestCase):
    def test_freeze_returns_metadata_and_writes_private_file(self):
        metadata = self.store.freeze(self.png)

        self.assertEqual(
            metadata,
            {
                "media_ref": f".harness/media/{self.sha}.png",
                "sha256": self.sha,
                "bytes": len(self.png),
                "media_type": "image/png",
                "width": 160,
                "height": 120,
            },
        )
        stored = self.root / metadata["media_ref"]
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), self.png)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)

    def test_freeze_jpeg_uses_jpg_extension(self):
        metadata = self.store.freeze(jpeg_bytes(20, 10))

        self.assertEqual(metadata["media_type"], "image/jpeg")
        self.assertTrue(metadata["media_ref"].endswith(".jpg"))
        self.assertEqual((metadata["width"], metadata["height"]), (20, 10))

    def test_identical_bytes_are_idempotent(self):
        first = self.store.freeze(self.png)
        second = self.store.freeze(self.png)

        self.assertEqual(first, second)
        self.assertEqual(sorted(path.name for path in self.media_dir.iterdir()), [f"{self.sha}.png"])

    def test_load_and_describe_match_frozen_metadata(self):
        metadata = self.store.freeze(self.png)

        self.assertEqual(self.store.load(metadata["media_ref"]), self.png)
        self.assertEqual(
            self.store.describe(metadata["media_ref"]),
            {
                "reference": metadata["media_ref"],
                "sha256": self.sha,
                "bytes": len(self.png),
                "media_type": "image/png",
                "width": 160,
                "height": 120,
            },
        )

    def test_load_rejects_non_reference(self):
        for reference in ("charts/a.png", f".harness/media/{self.sha}.gif", ".harness/media/../../secret.png"):
            with self.subTest(reference):
                with self.assertRaises(MediaError) as caught:
                    self.store.load(reference)
                self.assertEqual(media_code(caught.exception), "media_reference_invalid")


class AttachmentTests(MediaStoreTestCase):
    def test_accepted_attachment_survives_source_edit_and_delete(self):
        source = self._write_file("my chart.png", self.png)

        metadata = self.store.freeze_attachment("my chart.png", self.root)

        self.assertEqual(metadata["media_ref"], f".harness/media/{self.sha}.png")
        self.assertEqual(metadata["bytes"], len(self.png))

        source.write_bytes(render_bar_chart(["alpha", "beta"], [9, 9], width=160, height=120))
        self.assertEqual(self.store.load(metadata["media_ref"]), self.png)

        source.unlink()
        self.assertEqual(self.store.load(metadata["media_ref"]), self.png)

    def test_attachment_with_unicode_name_is_frozen(self):
        self._write_file("我的 图.png", self.png)

        metadata = self.store.freeze_attachment("我的 图.png", self.root)

        self.assertEqual(metadata["sha256"], self.sha)
        self.assertEqual(self.store.load(metadata["media_ref"]), self.png)

    def test_missing_attachment_reports_missing(self):
        with self.assertRaises(MediaError) as caught:
            self.store.freeze_attachment("absent.png", self.root)
        self.assertEqual(media_code(caught.exception), "media_missing")

    def test_attachment_outside_boundary_is_unauthorized(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name).resolve() / "chart.png"
        outside_path.write_bytes(self.png)

        for reference in (str(outside_path), "../outside.png"):
            with self.subTest(reference):
                with self.assertRaises(MediaError) as caught:
                    self.store.freeze_attachment(reference, self.root)
                self.assertEqual(media_code(caught.exception), "media_path_unauthorized")

    def test_private_runtime_path_is_unauthorized(self):
        self._write_file(".harness/protocol-state/frame.png", self.png)

        with self.assertRaises(MediaError) as caught:
            self.store.freeze_attachment(".harness/protocol-state/frame.png", self.root)

        self.assertEqual(media_code(caught.exception), "media_path_unauthorized")

    def test_attachment_bytes_limit_uses_snapshot_bound(self):
        self._write_file("chart.png", self.png)

        with self.assertRaises(MediaError) as caught:
            self.store.freeze_attachment("chart.png", self.root, MediaLimits(max_image_bytes=64))

        error = caught.exception
        self.assertEqual(media_code(error), "media_bytes_limit")
        self.assertEqual(limit_name(error), "image_bytes")
        self.assertEqual(error.details.get("limit_value"), 64)


class IntegrityTests(MediaStoreTestCase):
    def test_tampered_stored_bytes_fail_load(self):
        metadata = self.store.freeze(self.png)
        (self.root / metadata["media_ref"]).write_bytes(b"tampered content")

        with self.assertRaises(MediaError) as caught:
            self.store.load(metadata["media_ref"])

        self.assertEqual(media_code(caught.exception), "media_identity_conflict")

    def test_tampered_stored_bytes_fail_freeze(self):
        metadata = self.store.freeze(self.png)
        (self.root / metadata["media_ref"]).write_bytes(b"tampered content")

        with self.assertRaises(MediaError) as caught:
            self.store.freeze(self.png)

        self.assertEqual(media_code(caught.exception), "media_identity_conflict")

    def test_deleted_stored_bytes_report_reclaimed(self):
        metadata = self.store.freeze(self.png)
        (self.root / metadata["media_ref"]).unlink()

        with self.assertRaises(MediaError) as caught:
            self.store.load(metadata["media_ref"])

        self.assertEqual(media_code(caught.exception), "media_reclaimed")


class ValidateImageTests(MediaStoreTestCase):
    def assert_media_error(self, data, code, limits=None):
        with self.assertRaises(MediaError) as caught:
            validate_image(data, limits)
        self.assertEqual(media_code(caught.exception), code)
        return caught.exception

    def test_valid_png_and_jpeg(self):
        self.assertEqual(
            validate_image(self.png), {"media_type": "image/png", "width": 160, "height": 120}
        )
        self.assertEqual(
            validate_image(jpeg_bytes(20, 10)), {"media_type": "image/jpeg", "width": 20, "height": 10}
        )

    def test_empty_and_non_bytes(self):
        self.assert_media_error(b"", "media_empty")
        self.assert_media_error("not bytes", "media_empty")

    def test_wrong_magic(self):
        self.assert_media_error(b"BM" + b"\x00" * 32, "media_format_invalid")

    def test_truncated_png(self):
        self.assert_media_error(self.png[:-12], "media_truncated")

    def test_corrupted_png_crc(self):
        corrupted = bytearray(self.png)
        idat = corrupted.index(b"IDAT")
        corrupted[idat + 6] ^= 0xFF
        self.assert_media_error(bytes(corrupted), "media_format_invalid")

    def test_jpeg_missing_end_of_image(self):
        self.assert_media_error(jpeg_bytes()[:-2], "media_truncated")

    def test_jpeg_without_frame_header(self):
        self.assert_media_error(b"\xff\xd8\xff\xd9", "media_format_invalid")

    def test_per_image_bytes_limit(self):
        error = self.assert_media_error(self.png, "media_bytes_limit", MediaLimits(max_image_bytes=64))
        self.assertEqual(limit_name(error), "image_bytes")

    def test_per_side_dimension_limit(self):
        wide = render_bar_chart(["alpha", "beta"], [1, 2])
        error = self.assert_media_error(wide, "media_dimensions_limit", MediaLimits(max_dimension=512))
        self.assertEqual(limit_name(error), "image_dimension")
        self.assertEqual(error.details.get("observed_value"), 640)

    def test_pixel_limit(self):
        error = self.assert_media_error(self.png, "media_dimensions_limit", MediaLimits(max_pixels=1000))
        self.assertEqual(limit_name(error), "image_pixels")
        self.assertEqual(error.details.get("observed_value"), 160 * 120)


class RequestBodyTests(MediaStoreTestCase):
    def test_count_limit_counts_each_frozen_part(self):
        metadata = self.store.freeze(self.png)
        parts = [{"type": "image", "media": dict(metadata)} for _ in range(9)]

        with self.assertRaises(MediaError) as caught:
            request_body_bounds(parts, MediaLimits(max_images_per_request=8))

        self.assertEqual(media_code(caught.exception), "media_request_count_limit")

    def test_bounds_report_image_count_and_bytes(self):
        first = self.store.freeze(self.png)
        second = self.store.freeze(jpeg_bytes(20, 10))
        parts = [{"type": "image", "media": dict(first)}, {"type": "image", "media": dict(second)}]

        self.assertEqual(
            request_body_bounds(parts),
            {"images": 2, "image_bytes": len(self.png) + len(jpeg_bytes(20, 10))},
        )

    def test_total_image_bytes_limit(self):
        metadata = self.store.freeze(self.png)
        parts = [{"type": "image", "media": dict(metadata)} for _ in range(2)]

        with self.assertRaises(MediaError) as caught:
            request_body_bounds(parts, MediaLimits(max_request_image_bytes=len(self.png)))

        self.assertEqual(media_code(caught.exception), "media_request_bytes_limit")

    def test_unfrozen_part_is_not_submittable(self):
        parts = [{"type": "image", "path": "charts/a.png"}]

        with self.assertRaises(MediaError) as caught:
            request_body_bounds(parts)

        self.assertEqual(media_code(caught.exception), "media_reference_invalid")

    def test_request_body_size_has_its_own_code(self):
        limits = MediaLimits(max_request_body_bytes=1024)

        self.assertEqual(ensure_request_body_size(1024, limits), None)
        with self.assertRaises(MediaError) as caught:
            ensure_request_body_size(1025, limits)

        self.assertEqual(media_code(caught.exception), "media_request_body_limit")
        self.assertEqual(caught.exception.details.get("limit_value"), 1024)


class ExportTests(MediaStoreTestCase):
    def test_export_writes_copy_with_metadata(self):
        metadata = self.store.freeze(self.png)
        (self.root / "out").mkdir()

        result = self.store.export(metadata["media_ref"], "out/chart.png", self.root)

        destination = self.root / "out" / "chart.png"
        self.assertEqual(destination.read_bytes(), self.png)
        self.assertEqual(result["reference"], metadata["media_ref"])
        self.assertEqual(result["sha256"], self.sha)
        self.assertEqual(result["bytes"], len(self.png))
        self.assertIn("path", result)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

    def test_existing_destination_is_rejected(self):
        metadata = self.store.freeze(self.png)
        existing = self._write_file("out/chart.png", b"previous export")

        with self.assertRaises(MediaError) as caught:
            self.store.export(metadata["media_ref"], "out/chart.png", self.root)

        self.assertEqual(media_code(caught.exception), "media_export_exists")
        self.assertEqual(existing.read_bytes(), b"previous export")

    def test_unauthorized_destination_is_rejected(self):
        metadata = self.store.freeze(self.png)

        for destination in (".harness/media/copy.png", "../outside.png"):
            with self.subTest(destination):
                with self.assertRaises(MediaError) as caught:
                    self.store.export(metadata["media_ref"], destination, self.root)
                self.assertEqual(media_code(caught.exception), "media_export_unauthorized")

    def test_export_unknown_reference_is_rejected(self):
        with self.assertRaises(MediaError) as caught:
            self.store.export(f".harness/media/{'0' * 64}.png", "out/chart.png", self.root)

        self.assertEqual(media_code(caught.exception), "media_reclaimed")


if __name__ == "__main__":
    unittest.main()