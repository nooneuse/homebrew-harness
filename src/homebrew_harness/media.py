"""Frozen local image media: validation, content-addressed storage, export.

Issue #2 images-first. Accepted image bytes are frozen under
``.harness/media/<sha256>.<ext>`` through SafeFile (mode 0600) and registered
in the artifact inventory. Session history references keep frozen content
GC-protected for as long as a valid session/resume reference holds it, and
later edits or deletion of the original source file never change an accepted
turn. Input attachments do not depend on Bash output policy; tool-produced
media keeps the existing publication authorization.

Limit classes are reported separately: per-image bytes, images per request,
total request image bytes, encoded request-body bytes, and pixel/dimension
bounds. Base64 payloads are never counted as ordinary text context characters.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.runtime.errors import annotate_error
from homebrew_harness.security.file_visibility import assert_model_write, visible
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.artifact_inventory import register_artifact

MEDIA_REF = re.compile(r"^\.harness/media/([0-9a-f]{64})\.(png|jpg)$")
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}
# Frozen content was validated under its acceptance-time profile limits; reads
# use a fixed generous cap so a stricter current profile cannot hide history.
_MAX_FROZEN_BYTES = 64 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MediaError(ValueError):
    """A media operation failed with a stable, reportable code."""

    def __init__(self, message, code="media_invalid", **details):
        details.setdefault("retryable", False)
        if "path" in details:
            details["file_path"] = details.pop("path")
        super().__init__(message)
        annotate_error(self, code=code, message=message, **details)
        self.media_code = code


@dataclass(frozen=True)
class MediaLimits:
    """Separate, clearly reported media limit classes (issue #2)."""

    max_image_bytes: int = 8 * 1024 * 1024
    max_images_per_request: int = 8
    max_request_image_bytes: int = 24 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_dimension: int = 8192
    # Encoded JSON request-body ceiling. Raw image bytes grow ~4/3 under
    # base64; this bound leaves headroom for text, tools and JSON framing.
    max_request_body_bytes: int = 36 * 1024 * 1024


def _check_dimensions(width, height, limits):
    if width <= 0 or height <= 0:
        raise MediaError("Image dimensions are invalid.", "media_format_invalid")
    if width > limits.max_dimension or height > limits.max_dimension:
        raise MediaError(
            f"Image dimension {width}x{height} exceeds the per-side limit "
            f"of {limits.max_dimension} pixels.",
            "media_dimensions_limit",
            limit_name="image_dimension",
            limit_value=limits.max_dimension,
            observed_value=max(width, height),
            unit="pixels",
        )
    if width * height > limits.max_pixels:
        raise MediaError(
            f"Image pixel count {width * height} exceeds the limit "
            f"of {limits.max_pixels} pixels.",
            "media_dimensions_limit",
            limit_name="image_pixels",
            limit_value=limits.max_pixels,
            observed_value=width * height,
            unit="pixels",
        )


def _png_dimensions(data, limits):
    if len(data) < len(_PNG_SIGNATURE) + 25 or data[12:16] != b"IHDR":
        raise MediaError("PNG data is truncated or malformed.", "media_truncated")
    width, height = struct.unpack(">II", data[16:24])
    _check_dimensions(width, height, limits)
    offset = len(_PNG_SIGNATURE)
    seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise MediaError("PNG chunk data is truncated.", "media_truncated")
        payload = data[offset + 4 : offset + 8 + length]
        stored_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(payload) != stored_crc:
            raise MediaError(
                f"PNG chunk {kind.decode('ascii', 'replace')!r} failed its CRC check.",
                "media_format_invalid",
            )
        offset = end
        if kind == b"IEND":
            seen_iend = True
            break
    if not seen_iend:
        raise MediaError("PNG data is missing its IEND chunk.", "media_truncated")
    return width, height


def _jpeg_dimensions(data, limits):
    offset = 2
    width = height = None
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise MediaError("JPEG marker stream is malformed.", "media_format_invalid")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:  # EOI
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers
            continue
        if offset + 2 > len(data):
            raise MediaError("JPEG segment length is truncated.", "media_truncated")
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            raise MediaError("JPEG segment length is invalid.", "media_truncated")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                raise MediaError("JPEG frame header is truncated.", "media_truncated")
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
        if marker == 0xDA:  # SOS: entropy-coded scan follows until EOI
            break
        offset += length
    if width is None or height is None:
        raise MediaError("JPEG data has no readable frame header.", "media_format_invalid")
    if data[-2:] != b"\xff\xd9":
        raise MediaError("JPEG data is missing its end-of-image marker.", "media_truncated")
    _check_dimensions(width, height, limits)
    return width, height


def validate_image(data: bytes, limits: MediaLimits | None = None) -> dict:
    """Verify real PNG/JPEG bytes and return ``media_type/width/height``.

    Raises MediaError with distinct codes for empty, oversized, unsupported,
    malformed, truncated or out-of-bounds images. Never guesses from a file
    name or extension.
    """
    limits = limits or MediaLimits()
    if not isinstance(data, bytes) or not data:
        raise MediaError("Image data is empty.", "media_empty")
    if len(data) > limits.max_image_bytes:
        raise MediaError(
            f"Image is {len(data)} bytes, above the per-image limit "
            f"of {limits.max_image_bytes} bytes.",
            "media_bytes_limit",
            limit_name="image_bytes",
            limit_value=limits.max_image_bytes,
            observed_value=len(data),
            unit="bytes",
        )
    if data.startswith(_PNG_SIGNATURE):
        width, height = _png_dimensions(data, limits)
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data, limits)
        media_type = "image/jpeg"
    else:
        raise MediaError(
            "Unsupported image format: only real PNG and JPEG bytes are accepted.",
            "media_format_invalid",
        )
    return {"media_type": media_type, "width": width, "height": height}


class MediaStore:
    """Content-addressed frozen media under ``.harness/media`` in a workspace.

    Freezing is idempotent by content hash: identical bytes reuse the same
    reference; a hash collision with different bytes is an explicit conflict,
    never a silent overwrite. References held by valid session state are
    protected from GC by the retention graph's reference scan.
    """

    def __init__(self, root):
        self.root = Path(root)

    def freeze(self, data: bytes, limits: MediaLimits | None = None) -> dict:
        """Validate and freeze bytes; return the stable media reference dict."""
        execution_check()
        limits = limits or MediaLimits()
        meta = validate_image(data, limits)
        sha = hashlib.sha256(data).hexdigest()
        rel = f".harness/media/{sha}.{_EXTENSIONS[meta['media_type']]}"
        with SafeFile(self.root, rel, create_parents=True) as target:
            try:
                existing, version, _ = target.snapshot(max_bytes=_MAX_FROZEN_BYTES)
            except ValueError as exc:
                if getattr(exc, "code", None) == "file_snapshot_bytes_limit":
                    raise MediaError(
                        "Stored media exceeds the frozen-media read bound; the store entry is corrupt.",
                        "media_store_corrupt",
                        path=rel,
                    ) from exc
                raise
            if existing is not None:
                if hashlib.sha256(existing).hexdigest() != sha:
                    raise MediaError(
                        "Stored media does not match its content identity.",
                        "media_identity_conflict",
                        path=rel,
                    )
            else:
                try:
                    target.publish(data, version, mode=0o600)
                except FileExistsError:
                    # A concurrent freeze of identical content won the exclusive
                    # publication. Verify the durable bytes before treating the
                    # outcome as success; never blindly republish.
                    landed, _, _ = target.snapshot(max_bytes=_MAX_FROZEN_BYTES)
                    if landed is None or hashlib.sha256(landed).hexdigest() != sha:
                        raise MediaError(
                            "Stored media conflicts with the accepted content identity.",
                            "media_identity_conflict",
                            path=rel,
                        ) from None
        register_artifact(self.root, rel, data, "media")
        return {"media_ref": rel, "sha256": sha, "bytes": len(data), **meta}

    def freeze_attachment(self, path, workdir, limits: MediaLimits | None = None) -> dict:
        """Freeze a user attachment inside the authorized file boundary.

        Uses the same visibility boundary as model file reads; it does not
        require Bash output rules and never publishes through ArtifactStore.
        """
        execution_check()
        limits = limits or MediaLimits()
        workdir = Path(workdir)
        if not visible(workdir, str(path)):
            raise MediaError(
                "Attachment is outside the authorized file boundary or is private runtime data.",
                "media_path_unauthorized",
                path=str(path),
            )
        try:
            with SafeFile(workdir, str(path)) as target:
                data, _, _ = target.snapshot(max_bytes=limits.max_image_bytes)
        except FileNotFoundError as exc:
            raise MediaError("Attachment file does not exist.", "media_missing", path=str(path)) from exc
        except PermissionError as exc:
            raise MediaError("Attachment file is not readable.", "media_unreadable", path=str(path)) from exc
        except ValueError as exc:
            if getattr(exc, "code", None) == "file_snapshot_bytes_limit":
                raise MediaError(
                    f"Attachment exceeds the per-image limit of {limits.max_image_bytes} bytes.",
                    "media_bytes_limit",
                    path=str(path),
                    limit_name="image_bytes",
                    limit_value=limits.max_image_bytes,
                    unit="bytes",
                ) from exc
            raise MediaError(
                "Attachment path is not accessible inside the authorized boundary.",
                "media_path_unauthorized",
                path=str(path),
            ) from exc
        if data is None:
            raise MediaError("Attachment file does not exist.", "media_missing", path=str(path))
        return self.freeze(data, limits)

    def load(self, ref: str) -> bytes:
        """Read frozen bytes, verifying content identity; distinct reclaim error."""
        execution_check()
        match = MEDIA_REF.fullmatch(str(ref))
        if not match:
            raise MediaError("Not a frozen media reference.", "media_reference_invalid", path=str(ref))
        try:
            with SafeFile(self.root, str(ref)) as target:
                data, _, _ = target.snapshot(max_bytes=_MAX_FROZEN_BYTES)
        except FileNotFoundError as exc:
            raise MediaError(
                "Media content is no longer available (reclaimed or missing).",
                "media_reclaimed",
                path=str(ref),
            ) from exc
        if data is None:
            raise MediaError(
                "Media content is no longer available (reclaimed or missing).",
                "media_reclaimed",
                path=str(ref),
            )
        if hashlib.sha256(data).hexdigest() != match.group(1):
            raise MediaError(
                "Stored media does not match its content identity.",
                "media_identity_conflict",
                path=str(ref),
            )
        return data

    def describe(self, ref: str) -> dict:
        """Typed metadata for a frozen reference: type, bytes, hash, dimensions."""
        data = self.load(ref)
        meta = validate_image(data, MediaLimits(max_image_bytes=_MAX_FROZEN_BYTES))
        return {
            "reference": str(ref),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            **meta,
        }

    def export(self, ref: str, destination, workdir) -> dict:
        """Copy frozen bytes to a model-writable destination; never overwrite."""
        execution_check()
        data = self.load(ref)
        workdir = Path(workdir)
        try:
            assert_model_write(workdir, str(destination))
        except ValueError as exc:
            raise MediaError(
                "Export destination is outside the authorized file boundary or is host-managed.",
                "media_export_unauthorized",
                path=str(destination),
            ) from exc
        with SafeFile(workdir, str(destination), create_parents=True) as target:
            existing, version, _ = target.snapshot(max_bytes=_MAX_FROZEN_BYTES)
            if existing is not None:
                raise MediaError(
                    "Export destination already exists; choose a new path.",
                    "media_export_exists",
                    path=str(destination),
                )
            target.publish(data, version, 0o644)
        meta = validate_image(data, MediaLimits(max_image_bytes=_MAX_FROZEN_BYTES))
        return {
            "reference": str(ref),
            "path": str(destination),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            **meta,
        }


def request_body_bounds(parts, limits: MediaLimits | None = None) -> dict:
    """Check image count and total raw image bytes before provider submission.

    Count and byte limits are reported as distinct error codes. Parts must
    already be frozen (``media`` metadata present); unfrozen parts are an
    explicit error, never silently skipped.
    """
    limits = limits or MediaLimits()
    images = [
        part for part in parts
        if isinstance(part, dict) and part.get("type") == "image"
    ]
    total = 0
    for part in images:
        media = part.get("media")
        if not isinstance(media, dict) or not isinstance(media.get("bytes"), int):
            raise MediaError(
                "Image content part is not frozen; freeze attachments before submission.",
                "media_reference_invalid",
            )
        total += media["bytes"]
    if len(images) > limits.max_images_per_request:
        raise MediaError(
            f"Request carries {len(images)} images, above the limit "
            f"of {limits.max_images_per_request} images per request.",
            "media_request_count_limit",
            limit_name="images_per_request",
            limit_value=limits.max_images_per_request,
            observed_value=len(images),
            unit="images",
        )
    if total > limits.max_request_image_bytes:
        raise MediaError(
            f"Request carries {total} image bytes, above the limit "
            f"of {limits.max_request_image_bytes} bytes per request.",
            "media_request_bytes_limit",
            limit_name="request_image_bytes",
            limit_value=limits.max_request_image_bytes,
            observed_value=total,
            unit="bytes",
        )
    return {"images": len(images), "image_bytes": total}


def ensure_request_body_size(size: int, limits: MediaLimits | None = None) -> None:
    """Bound the encoded JSON request body separately from raw image bytes."""
    limits = limits or MediaLimits()
    if size > limits.max_request_body_bytes:
        raise MediaError(
            f"Encoded request body is {size} bytes, above the limit "
            f"of {limits.max_request_body_bytes} bytes.",
            "media_request_body_limit",
            limit_name="request_body_bytes",
            limit_value=limits.max_request_body_bytes,
            observed_value=size,
            unit="bytes",
        )
