"""Registered tool producing frozen PNG media artifacts (images-first output).

The first-phase image output path is tool-produced media only: a fixed,
host-registered renderer draws a bar chart with the standard library, freezes
the exact bytes through MediaStore (content-addressed, inventory-registered)
and returns a media-bearing result.  Publication stays inside the existing
media lifecycle; no Bash output policy or arbitrary code is involved.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from homebrew_harness.contracts import MEDIA_REF, history_media, media_descriptor
from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.media import MediaError, MediaStore, require_media_root
from homebrew_harness.security.data_boundary import redact

MAX_BARS = 24
MAX_LABEL_CHARS = 60
MAX_TITLE_CHARS = 80
MAX_VALUE = 1_000_000_000

# 5x7 bitmap font; chart text is ASCII-only.  Full labels and the title always
# travel in the JSON text result, so non-ASCII data is never lost.
_GLYPHS = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    " ": "00000 00000 00000 00000 00000 00000 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    ",": "00000 00000 00000 00000 01100 00100 01000",
    "/": "00001 00010 00010 00100 01000 01000 10000",
    "%": "11001 11010 00010 00100 01000 01011 10011",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
}
_FONT = {
    char: tuple(int(row, 2) for row in rows.split())
    for char, rows in _GLYPHS.items()
}

_PALETTE = (
    (66, 133, 244),
    (219, 68, 55),
    (244, 180, 0),
    (15, 157, 88),
    (171, 71, 188),
    (0, 172, 193),
)
_BACKGROUND = (255, 255, 255)
_INK = (32, 33, 36)
_AXIS = (95, 99, 104)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """Encode 8-bit RGB rows as a baseline PNG using only the standard library."""
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


class _Canvas:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.pixels = bytearray(bytes(_BACKGROUND) * (width * height))

    def rect(self, x: int, y: int, w: int, h: int, color) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        line = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            start = (yy * self.width + x0) * 3
            self.pixels[start : start + len(line)] = line

    def text(self, x: int, y: int, value: str, color, scale: int = 1) -> int:
        cursor = x
        for char in value.upper():
            glyph = _FONT.get(char)
            if glyph is None:
                continue
            for ry, bits in enumerate(glyph):
                for cx in range(5):
                    if bits & (1 << (4 - cx)):
                        self.rect(cursor + cx * scale, y + ry * scale, scale, scale, color)
            cursor += 6 * scale
        return cursor

    def text_width(self, value: str, scale: int = 1) -> int:
        return sum(6 * scale for char in value.upper() if char in _FONT)

    def rows(self) -> list[bytes]:
        stride = self.width * 3
        return [bytes(self.pixels[i : i + stride]) for i in range(0, len(self.pixels), stride)]


def render_bar_chart(
    labels: list[str],
    values: list[int],
    *,
    title: str | None = None,
    width: int = 640,
    height: int = 480,
) -> bytes:
    count = len(labels)
    canvas = _Canvas(width, height)
    top = 34 if title else 12
    bottom = 26
    max_value = max(values)
    left = max(34, canvas.text_width(str(max_value), 2) + 12)
    right = 12
    plot_w = width - left - right
    plot_h = height - top - bottom
    if plot_h < 40:
        raise ValueError("Chart height is too small for the plot area; use height >= 120.")
    gap = max(4, plot_w // (count * 5))
    bar_w = (plot_w - gap * (count + 1)) // count
    if bar_w < 6:
        raise ValueError(
            "Too many bars for the requested width; reduce labels or increase width."
        )
    if title:
        canvas.text(left, 8, title[: MAX_TITLE_CHARS], _INK, 2)
    # Axes.
    canvas.rect(left, top, 2, plot_h + 2, _AXIS)
    canvas.rect(left, top + plot_h, plot_w, 2, _AXIS)
    scale_max = max_value or 1
    for tick in dict.fromkeys((0, max_value // 2, max_value)):
        ty = top + plot_h - (tick * plot_h // scale_max)
        label = str(tick)
        canvas.rect(left - 6, ty, 6, 1, _AXIS)
        canvas.text(left - 10 - canvas.text_width(label, 1), ty - 3, label, _AXIS, 1)
    for index, (label, value) in enumerate(zip(labels, values)):
        execution_check()
        x = left + gap + index * (bar_w + gap)
        bar_h = value * plot_h // scale_max
        color = _PALETTE[index % len(_PALETTE)]
        canvas.rect(x, top + plot_h - bar_h, bar_w, bar_h, color)
        value_text = str(value)
        for scale in (2, 1):
            if canvas.text_width(value_text, scale) <= bar_w + gap - 2:
                canvas.text(
                    x + (bar_w - canvas.text_width(value_text, scale)) // 2,
                    top + plot_h - bar_h - 8 * scale,
                    value_text,
                    _INK,
                    scale,
                )
                break
        index_text = str(index + 1)
        canvas.text(
            x + (bar_w - canvas.text_width(index_text, 1)) // 2,
            top + plot_h + 6,
            index_text,
            _AXIS,
            1,
        )
    return encode_png(width, height, canvas.rows())


def run_render_chart(
    labels,
    values,
    workdir: Path,
    title=None,
    width: int = 640,
    height: int = 480,
    *,
    session_root: Path,
) -> str:
    """Render a bar chart and freeze it in the owning Session."""
    execution_check()
    root = require_media_root(session_root, workdir)
    if not isinstance(labels, list) or not isinstance(values, list):
        raise ValueError("labels and values must be arrays.")
    if not 1 <= len(labels) <= MAX_BARS or len(labels) != len(values):
        raise ValueError(
            f"render_chart needs 1-{MAX_BARS} labels with exactly one value per label."
        )
    if any(
        not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS
        for label in labels
    ):
        raise ValueError(
            f"Each chart label must be a string of at most {MAX_LABEL_CHARS} characters."
        )
    if any(type(value) is not int or value < 0 or value > MAX_VALUE for value in values):
        raise ValueError(f"values must be integers between 0 and {MAX_VALUE:,}.")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE_CHARS):
        raise ValueError(f"title must be a string of at most {MAX_TITLE_CHARS} characters.")
    if type(width) is not int or type(height) is not int or not (
        160 <= width <= 2048 and 120 <= height <= 2048
    ):
        raise ValueError("width must be an integer from 160-2048 and height from 120-2048 pixels.")
    # Apply the text data boundary before embedding any model-supplied text in
    # pixels; post-dispatch redaction cannot remove text from a frozen image.
    title = redact(title)
    labels = [redact(label) for label in labels]
    png = render_bar_chart(labels, values, title=title, width=width, height=height)
    execution_check()
    meta = MediaStore(root).freeze(png)
    summary = "; ".join(f"{label}={value}" for label, value in zip(labels, values))
    text = (
        f"Rendered bar chart{(' ' + title) if title else ''}: {summary}. "
        f"PNG {width}x{height}, {len(png)} bytes, frozen as {meta['media_ref']}."
    )
    return _media_result(text, meta)


def run_read_media(media_ref: str, workdir: Path, *, session_root: Path, messages) -> str:
    """Return the frozen pixels reference only if owned by this Session's history."""
    execution_check()
    root = require_media_root(session_root, workdir)
    if not isinstance(media_ref, str) or MEDIA_REF.fullmatch(media_ref) is None:
        raise MediaError("Supply an exact frozen media_ref from this session.", "media_reference_invalid")
    current = messages() if callable(messages) else messages
    owned = next((item for item in history_media(current) if item["media_ref"] == media_ref), None)
    if owned is None:
        # Do not probe the shared store or reveal if another session has it.
        raise MediaError("This session does not own that media reference.", "media_reference_unowned")
    descriptor = MediaStore(root).descriptor(media_ref)
    if descriptor != media_descriptor(owned):
        raise MediaError("Stored media no longer matches the session descriptor.", "media_identity_conflict")
    return _media_result(
        f"Read frozen {descriptor['media_type']} {descriptor['width']}x{descriptor['height']}, "
        f"{descriptor['bytes']} bytes, from {media_ref}.",
        descriptor,
    )


def _media_result(text: str, descriptor: dict) -> str:
    return json.dumps({"text": redact(text), "media": [descriptor]})
