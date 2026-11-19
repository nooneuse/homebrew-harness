"""Small, serializable host contracts, independent of internal execution objects."""

from dataclasses import asdict, dataclass, field

from homebrew_harness.security.data_boundary import redact

import hashlib
import json
import re
from typing import Any

# --- Ordered multimodal content parts (issue #2, images-first) -------------
# A submission is either a plain string (text-only, unchanged behavior) or an
# ordered list of content parts. Text part: {"type": "text", "text": str}.
# Image part (frozen): {"type": "image", "media": {media_ref, media_type, bytes,
# sha256, width, height}}. An unfrozen image part carries {"path": str} and is
# frozen by the session before acceptance. Part order is preserved end to end.
TEXT_PART = "text"
IMAGE_PART = "image"


def normalize_content_parts(content: Any) -> list[dict]:
    """Normalize a string or ordered part list into serializable content parts.

    Plain strings keep working unchanged. Image parts may be unfrozen
    (``path``) or already frozen (``media``); freezing happens at acceptance.
    """
    if isinstance(content, str):
        return [{"type": TEXT_PART, "text": content}]
    if not isinstance(content, (list, tuple)):
        raise InvalidInput("Content must be a string or an ordered list of content parts.")
    parts: list[dict] = []
    for item in content:
        if isinstance(item, str):
            parts.append({"type": TEXT_PART, "text": item})
            continue
        if not isinstance(item, dict):
            raise InvalidInput("Each content part must be a string or an object.")
        kind = item.get("type")
        if kind == TEXT_PART:
            text = item.get("text")
            if not isinstance(text, str):
                raise InvalidInput("A text content part requires string 'text'.")
            parts.append({"type": TEXT_PART, "text": text})
        elif kind == IMAGE_PART:
            if "media" in item:
                media = item["media"]
                if not isinstance(media, dict) or not media.get("media_ref") or not media.get("sha256"):
                    raise InvalidInput("A frozen image part requires media_ref and sha256.")
                parts.append({"type": IMAGE_PART, "media": dict(media)})
            elif "path" in item:
                path = item["path"]
                if not isinstance(path, str) or not path:
                    raise InvalidInput("An image content part requires a non-empty 'path'.")
                parts.append({"type": IMAGE_PART, "path": path})
            else:
                raise InvalidInput("An image content part requires 'path' or frozen 'media'.")
        else:
            raise InvalidInput(f"Unsupported content part type: {kind!r}.")
    if not parts:
        raise InvalidInput("Content must contain at least one part.")
    return parts


def _part_items(parts) -> tuple:
    """Iterate dict parts only; plain strings/None carry no media parts."""
    if isinstance(parts, (list, tuple)):
        return tuple(part for part in parts if isinstance(part, dict))
    return ()


def content_is_multimodal(parts) -> bool:
    return any(part.get("type") == IMAGE_PART for part in _part_items(parts))


def content_text(parts) -> str:
    """Concatenate text parts for display/title/estimate; never media bytes."""
    if isinstance(parts, str):
        return parts
    return "\n".join(
        part["text"]
        for part in _part_items(parts)
        if part.get("type") == TEXT_PART and isinstance(part.get("text"), str)
    )


def content_media(parts) -> list[dict]:
    return [
        part["media"]
        for part in _part_items(parts)
        if part.get("type") == IMAGE_PART and isinstance(part.get("media"), dict)
    ]


def content_media_refs(parts) -> list[str]:
    return [media["media_ref"] for media in content_media(parts)]


def content_identity(parts) -> str:
    """Stable digest of ordered parts: text, media content identity and order.

    Repeated-request deduplication compares this so two submissions match only
    when their text, media content, order and required metadata all match.
    """
    payload = []
    if isinstance(parts, str):
        parts = [{"type": TEXT_PART, "text": parts}]
    for part in _part_items(parts):
        if part.get("type") == TEXT_PART:
            payload.append(["text", part["text"]])
        elif part.get("type") == IMAGE_PART:
            media = part.get("media", {})
            if isinstance(media, dict) and media.get("sha256"):
                payload.append([
                    "image", media.get("sha256"), media.get("media_type"),
                    media.get("bytes"), media.get("width"), media.get("height"),
                ])
            else:
                # Unfrozen attachment: identity is the referenced path until
                # acceptance freezes real content identity.
                payload.append(["image-path", part.get("path")])
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Flat content-addressed layout; must stay in sync with media.MEDIA_REF.
_MEDIA_REF_RE = re.compile(r"^\.harness/media/([0-9a-f]{64})\.(png|jpg)$")
_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg"}


def _media_descriptor(item: Any) -> dict | None:
    """Validate one frozen media descriptor; ``None`` when malformed."""
    if not isinstance(item, dict):
        return None
    ref = item.get("media_ref")
    match = _MEDIA_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    digest, extension = match.groups()
    if (
        item.get("media_type") != _MEDIA_TYPES[extension]
        or item.get("sha256") != digest
        or type(item.get("bytes")) is not int
        or not 0 < item["bytes"] <= 64 * 1024 * 1024
        or type(item.get("width")) is not int
        or item["width"] <= 0
        or type(item.get("height")) is not int
        or item["height"] <= 0
    ):
        return None
    return {
        "media_ref": ref,
        "media_type": item["media_type"],
        "bytes": item["bytes"],
        "sha256": digest,
        "width": item["width"],
        "height": item["height"],
    }


def tool_result_media(output: Any) -> list[dict]:
    """Extract validated frozen media descriptors from a tool result.

    A media-bearing tool result is a JSON object with a ``media`` list of
    frozen descriptors (as produced by MediaStore.freeze) alongside optional
    text. Error results, malformed descriptors and non-media results yield an
    empty list; callers must never treat filenames or byte text as images.
    """
    if isinstance(output, str):
        if '"media"' not in output:
            return []
        try:
            output = json.loads(output)
        except (TypeError, ValueError):
            return []
    if not isinstance(output, dict) or output.get("error") is not None:
        return []
    media = output.get("media")
    if not isinstance(media, list) or not media:
        return []
    result = []
    for item in media:
        descriptor = _media_descriptor(item)
        if descriptor is None:
            return []
        result.append(descriptor)
    return result


RETAINED_MEDIA_HEADER = (
    "Retained media (frozen content references from archived history; "
    "retrieve on demand through the session media CLI/API, never by "
    "re-reading edited source files):"
)


def compaction_marker_media(content: Any) -> list[dict]:
    """Frozen descriptors retained in a compaction marker's media inventory.

    Compaction archives old turns into a named marker but keeps their media
    references retrievable on demand; parsing that inventory lets session
    media listing survive history compaction. Only the harness compaction
    marker is trusted; malformed inventories yield an empty list.
    """
    if not isinstance(content, str) or RETAINED_MEDIA_HEADER not in content:
        return []
    lines = content.split("\n")
    for index, line in enumerate(lines):
        if line.strip() != RETAINED_MEDIA_HEADER:
            continue
        if index + 1 >= len(lines):
            return []
        try:
            inventory = json.loads(lines[index + 1])
        except (TypeError, ValueError):
            return []
        if not isinstance(inventory, list):
            return []
        result = []
        for item in inventory:
            descriptor = _media_descriptor(item)
            origin = item.get("origin") if isinstance(item, dict) else None
            if descriptor is None or origin not in {"user_input", "tool_result"}:
                return []
            result.append({**descriptor, "origin": origin})
        return result
    return []


def history_media(messages) -> list[dict]:
    """Ordered, deduplicated media descriptors referenced by session history.

    User attachments and tool-produced media results are distinguished by
    ``origin``; model text output never carries media descriptors. Descriptors
    keep their frozen identity (reference, media type, bytes, hash, size) so
    callers can retrieve metadata without scraping text output.
    """
    seen: dict[str, dict] = {}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "tool"}:
            continue
        content = message.get("content")
        if role == "tool":
            descriptors = tool_result_media(content)
            origin = "tool_result"
        elif message.get("name") == "harness_compaction" and isinstance(content, str):
            # Archived turns keep their frozen media in the marker inventory.
            for media in compaction_marker_media(content):
                ref = media["media_ref"]
                if ref not in seen:
                    seen[ref] = media
            continue
        else:
            descriptors = content_media(content)
            origin = "user_input"
        for media in descriptors:
            ref = media.get("media_ref")
            if isinstance(ref, str) and ref and ref not in seen:
                seen[ref] = {**media, "origin": origin}
    return list(seen.values())


_IMAGE_MARKUP = re.compile(r"\[image:(\"[^\"]*\"|'[^']*'|[^\]\s]+)\]")


def parse_image_markup(text: str):
    """Split CLI input into ordered text/image parts around ``[image:path]``.

    Paths containing spaces or brackets can be quoted: ``[image:"my 图.png"]``.
    Returns ``None`` when no markup is present so plain-text submissions stay
    byte-for-byte unchanged; otherwise returns ordered parts with unfrozen
    image paths (the session freezes them at acceptance).
    """
    if not isinstance(text, str) or "[image:" not in text:
        return None
    matches = list(_IMAGE_MARKUP.finditer(text))
    # Any "[image:" occurrence outside a well-formed match is malformed
    # markup (empty path, stray whitespace, unclosed bracket) and must fail
    # loudly instead of silently degrading to plain text.
    probe = text.find("[image:")
    while probe != -1:
        if not any(m.start() <= probe < m.end() for m in matches):
            raise InvalidInput("An [image:...] attachment requires a non-empty path.")
        probe = text.find("[image:", probe + 1)
    parts: list[dict] = []
    pos = 0
    for match in matches:
        before = text[pos:match.start()].strip()
        if before:
            parts.append({"type": TEXT_PART, "text": before})
        raw = match.group(1)
        path = raw[1:-1] if raw[:1] in {'"', "'"} else raw
        if not path:
            raise InvalidInput("An [image:...] attachment requires a non-empty path.")
        parts.append({"type": IMAGE_PART, "path": path})
        pos = match.end()
    tail = text[pos:].strip()
    if tail:
        parts.append({"type": TEXT_PART, "text": tail})
    if not parts:
        raise InvalidInput("Image markup did not contain any content.")
    return parts


class SDKError(RuntimeError):
    def __init__(self, message, *, code="sdk_error", ref=None, details=None):
        super().__init__(redact(message))
        self.code, self.ref, self.details = code, ref, redact(details or {})


class UnsupportedOperation(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="unsupported_operation", **kw)


class InvalidInput(SDKError, ValueError):
    def __init__(self, message, **kw):
        super().__init__(message, code="invalid_input", **kw)


class StaleReference(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="stale_reference", **kw)


class ResultNotReady(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="result_not_ready", **kw)


class ClosePending(SDKError):
    def __init__(self, report):
        super().__init__(
            "Owned work remains pending after the close window.",
            code="cleanup_pending",
            details=report.data(),
        )
        self.report = report


class Data:
    def data(self):
        return redact(asdict(self))


@dataclass(frozen=True)
class ExecutionRef(Data):
    kind: str
    id: str
    owner_id: str | None = None


@dataclass(frozen=True)
class SubmissionReceipt(Data):
    ref: ExecutionRef
    request_id: str
    accepted: bool
    launch_state: str = "accepted"


@dataclass(frozen=True)
class RequestRef(Data):
    id: str
    revision: int
    target: ExecutionRef
    durable: bool = True

    def __post_init__(self):
        if (
            not isinstance(self.id, str)
            or not self.id
            or type(self.revision) is not int
            or self.revision < 0
            or not isinstance(self.target, ExecutionRef)
            or type(self.durable) is not bool
        ):
            raise InvalidInput(
                "Request references require an ID, nonnegative revision and execution target."
            )


@dataclass(frozen=True)
class HumanRequest(Data):
    ref: RequestRef
    kind: str
    state: str
    tool: str | None = None
    arguments: dict | None = None
    question: str = ""
    reason: str = ""
    expires_at: float | None = None
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionSnapshot(Data):
    ref: ExecutionRef
    state: str
    revision: int = 0
    reason: str = ""
    input_revision: int = 0
    requests: tuple = ()
    output_available: bool = False
    persistence_confirmed: bool = False
    execution_pending: bool = False
    cleanup: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def terminal(self):
        return self.state in {
            "completed",
            "delivered",
            "failed",
            "cancelled",
            "abandoned",
            "stopped",
        }

    @property
    def settled(self):
        return (
            (self.terminal or self.state == "paused")
            and self.persistence_confirmed
            and not self.execution_pending
        )

    @property
    def actionable(self):
        return (
            self.settled
            or self.state
            in {
                "paused",
                "blocked",
                "waiting",
                "waiting_input",
                "needs_attention",
                "startup_pending",
                "unattached",
                "review",
            }
            or bool(self.requests)
        )


@dataclass(frozen=True)
class ExecutionResult(Data):
    ref: ExecutionRef
    state: str
    text: str | None = None
    value: object = None
    error: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WaitReport(Data):
    snapshot: ExecutionSnapshot
    condition_met: bool
    timed_out: bool = False


@dataclass(frozen=True)
class ExecutionEvent(Data):
    id: str
    kind: str
    snapshot: ExecutionSnapshot


@dataclass(frozen=True)
class CloseReport(Data):
    state: str
    persistence_ok: bool
    resources_closed: bool
    pending: tuple = ()
    errors: tuple = ()
    pending_calls: int = 0
