"""Frozen local PNG/JPEG storage and request bounds.

Accepted bytes are stored with mode 0600 under a content-addressed name in
``.harness/media``. This is a host storage primitive, not an authorization
check: callers exposing a stored reference must first establish ownership
from the current session history. No original file is consulted on a read.
"""

from __future__ import annotations

import errno
import hashlib
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

from homebrew_harness.contracts import (
    MAX_FROZEN_MEDIA_BYTES,
    MEDIA_REF,
    MEDIA_TYPES_BY_EXTENSION,
    media_descriptor,
)
from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.runtime.errors import annotate_error
from homebrew_harness.security.file_visibility import (
    host_managed_parts,
    model_parts,
    private_parts,
)
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.artifact_inventory import register_artifact

_EXTENSIONS = {media_type: ext for ext, media_type in MEDIA_TYPES_BY_EXTENSION.items()}
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


# The fixed storage ceiling applies even if a request profile is looser.
_STORAGE_LIMITS = MediaLimits(max_image_bytes=MAX_FROZEN_MEDIA_BYTES)


def require_media_root(session_root, workdir) -> Path:
    """Require the active tool workspace to be the exact host session root."""
    try:
        if session_root is not None:
            root = canonical_root(Path(session_root))
            if root == canonical_root(Path(workdir)):
                return root
    except (OSError, TypeError, ValueError, RuntimeError):
        pass
    raise MediaError(
        "Media is only available in the owning session root, not a different worktree.",
        "media_scope_unsupported",
    )


def _ordinary_path(workdir, path) -> tuple[Path, str]:
    root, parts = model_parts(workdir, str(path))
    if host_managed_parts(parts) or private_parts(parts) or any(
        part.casefold() == ".worktrees" for part in parts
    ):
        raise ValueError("Managed workspaces and private runtime paths are not media sources or destinations.")
    return root, Path(*parts).as_posix()


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
    if len(data) < len(_PNG_SIGNATURE) + 25:
        raise MediaError("PNG header is truncated.", "media_truncated")
    if data[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise MediaError("PNG has no valid IHDR chunk.", "media_format_invalid")
    width, height, depth, color, compression, filt, interlace = struct.unpack_from(
        ">IIBBBBB", data, 16
    )
    _check_dimensions(width, height, limits)
    allowed_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if depth not in allowed_depths.get(color, ()) or compression or filt or interlace not in (0, 1):
        raise MediaError("PNG header parameters are invalid.", "media_format_invalid")
    offset = len(_PNG_SIGNATURE)
    image_bytes = 0
    view = memoryview(data)
    while offset + 12 <= len(data):
        execution_check()
        length = struct.unpack_from(">I", view, offset)[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise MediaError("PNG chunk data is truncated.", "media_truncated")
        stored_crc = struct.unpack_from(">I", view, end - 4)[0]
        if zlib.crc32(view[offset + 4 : end - 4]) != stored_crc:
            raise MediaError(
                f"PNG chunk {kind.decode('ascii', 'replace')!r} failed its CRC check.",
                "media_format_invalid",
            )
        if kind == b"IHDR" and offset != len(_PNG_SIGNATURE):
            raise MediaError("PNG contains a duplicate header.", "media_format_invalid")
        if kind == b"IDAT":
            image_bytes += length
        if kind == b"IEND":
            if length or end != len(data) or not image_bytes:
                raise MediaError("PNG image data or trailer is invalid.", "media_format_invalid")
            return width, height
        offset = end
    raise MediaError("PNG data is missing its IEND chunk.", "media_truncated")


def _jpeg_dimensions(data, limits):
    offset = 2
    width = height = None
    while offset + 4 <= len(data):
        execution_check()
        if data[offset] != 0xFF:
            raise MediaError("JPEG marker stream is malformed.", "media_format_invalid")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:  # EOI before any scan
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers
            continue
        if offset + 2 > len(data):
            raise MediaError("JPEG segment length is truncated.", "media_truncated")
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data):
            raise MediaError("JPEG segment length is invalid.", "media_truncated")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 11:
                raise MediaError("JPEG frame header is truncated.", "media_truncated")
            if width is not None or length != 8 + 3 * data[offset + 7]:
                raise MediaError("JPEG frame header is malformed.", "media_format_invalid")
            height, width = struct.unpack_from(">HH", data, offset + 3)
            _check_dimensions(width, height, limits)
        if marker == 0xDA:  # SOS: require a frame and payload before the EOI
            if width is None or length < 8 or length != 6 + 2 * data[offset + 2]:
                raise MediaError("JPEG scan header is malformed.", "media_format_invalid")
            if data[-2:] != b"\xff\xd9" or offset + length >= len(data) - 2:
                raise MediaError("JPEG scan or end-of-image marker is truncated.", "media_truncated")
            return width, height
        offset += length
    raise MediaError("JPEG has no readable frame and scan.", "media_format_invalid")


def validate_image(data: bytes, limits: MediaLimits | None = None) -> dict:
    """Check bounded PNG/JPEG structure and return ``media_type/width/height``.

    This identifies bytes, not a filename or extension, and does not decode
    pixel data. Raises MediaError for unsupported, malformed, truncated, or
    out-of-bound content detected by these checks.
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
    """Content-addressed frozen media under the host-selected session root."""

    def __init__(self, root):
        self.root = Path(root)

    def freeze(self, data: bytes, limits: MediaLimits | None = None) -> dict:
        """Validate and freeze bytes; return the stable media reference dict."""
        execution_check()
        limits = limits or MediaLimits()
        meta = validate_image(data, replace(
            limits,
            max_image_bytes=min(limits.max_image_bytes, _STORAGE_LIMITS.max_image_bytes),
            max_dimension=min(limits.max_dimension, _STORAGE_LIMITS.max_dimension),
            max_pixels=min(limits.max_pixels, _STORAGE_LIMITS.max_pixels),
        ))
        sha = hashlib.sha256(data).hexdigest()
        rel = f".harness/media/{sha}.{_EXTENSIONS[meta['media_type']]}"
        with SafeFile(self.root, rel, create_parents=True) as target:
            existing, version, mode = self._snapshot(target, rel)
            if existing is None:
                try:
                    target.publish(data, version, mode=0o600)
                    existing, mode = data, 0o600
                except (FileExistsError, ValueError):
                    # The other publisher can win either the version recheck or
                    # the exclusive link. Accept only the identical private file.
                    existing, _, mode = self._snapshot(target, rel)
                    if existing is None:
                        raise
            if existing != data or mode != 0o600:
                raise MediaError(
                    "Stored media is not the expected private content.",
                    "media_identity_conflict",
                    path=rel,
                )
        register_artifact(self.root, rel, data, "media")
        return {"media_ref": rel, "sha256": sha, "bytes": len(data), **meta}

    @staticmethod
    def _snapshot(target, ref):
        try:
            return target.snapshot(max_bytes=MAX_FROZEN_MEDIA_BYTES)
        except ValueError as exc:
            raise MediaError(
                "Stored media cannot be read as a bounded regular file.",
                "media_store_corrupt",
                path=ref,
            ) from exc

    def freeze_attachment(self, path, workdir, limits: MediaLimits | None = None) -> dict:
        """Freeze an ordinary file inside the authorized session root."""
        execution_check()
        limits = limits or MediaLimits()
        root = require_media_root(self.root, workdir)
        byte_limit = min(limits.max_image_bytes, MAX_FROZEN_MEDIA_BYTES)
        try:
            root, relative = _ordinary_path(root, path)
            with SafeFile(root, relative) as target:
                data, _, _ = target.snapshot(max_bytes=byte_limit)
        except FileNotFoundError as exc:
            raise MediaError("Attachment file does not exist.", "media_missing", path=str(path)) from exc
        except PermissionError as exc:
            raise MediaError("Attachment file is not readable.", "media_unreadable", path=str(path)) from exc
        except (OSError, ValueError) as exc:
            if getattr(exc, "code", None) == "file_snapshot_bytes_limit":
                raise MediaError(
                    f"Attachment exceeds the per-image limit of {byte_limit} bytes.",
                    "media_bytes_limit",
                    path=str(path),
                    limit_name="image_bytes",
                    limit_value=byte_limit,
                    observed_value=exc.details.get("observed_value"),
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
        """Read frozen bytes and verify their content-addressed identity."""
        execution_check()
        match = MEDIA_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None:
            raise MediaError("Not a frozen media reference.", "media_reference_invalid")
        try:
            with SafeFile(self.root, ref) as target:
                data, _, mode = self._snapshot(target, ref)
        except FileNotFoundError:
            data = None
        except OSError as exc:
            raise MediaError("Stored media is inaccessible.", "media_store_corrupt", path=ref) from exc
        if data is None:
            raise MediaError(
                "Media content is no longer available (reclaimed or missing).",
                "media_reclaimed",
                path=ref,
            )
        signature = _PNG_SIGNATURE if match.group(2) == "png" else b"\xff\xd8"
        if mode != 0o600 or not data.startswith(signature) or hashlib.sha256(data).hexdigest() != match.group(1):
            raise MediaError(
                "Stored media is not the expected private content.",
                "media_identity_conflict",
                path=ref,
            )
        return data

    @staticmethod
    def _descriptor(ref, data):
        return {
            "media_ref": ref,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            **validate_image(data, _STORAGE_LIMITS),
        }

    def descriptor(self, ref: str) -> dict:
        """Recompute authoritative metadata for use in trusted media results."""
        return self._descriptor(ref, self.load(ref))

    def describe(self, ref: str) -> dict:
        return self._description(self.descriptor(ref))

    @staticmethod
    def _description(descriptor):
        return {"reference": descriptor["media_ref"], **{
            key: value for key, value in descriptor.items() if key != "media_ref"
        }}

    def export(self, ref: str, destination, workdir) -> dict:
        """Copy frozen bytes to an ordinary path; never overwrite."""
        execution_check()
        root = require_media_root(self.root, workdir)
        try:
            root, relative = _ordinary_path(root, destination)
        except (OSError, ValueError) as exc:
            raise MediaError(
                "Export destination is outside the authorized file boundary or is host-managed.",
                "media_export_unauthorized",
                path=str(destination),
            ) from exc
        data = self.load(ref)
        descriptor = self._descriptor(ref, data)
        try:
            with SafeFile(root, relative, create_parents=True) as target:
                # SafeFile never overwrites with an absent expected version.
                target.publish(data, None, 0o644)
        except (FileExistsError, ValueError) as exc:
            raise MediaError(
                "Export destination already exists or changed; choose a new path.",
                "media_export_exists",
                path=str(destination),
            ) from exc
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            raise MediaError(
                "Export destination is not accessible inside the authorized boundary.",
                "media_export_unauthorized",
                path=str(destination),
            ) from exc
        return {**self._description(descriptor), "path": str(destination)}


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
        media = media_descriptor(part.get("media"))
        if media is None:
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
