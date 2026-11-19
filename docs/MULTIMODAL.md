# Multimodal sessions (images first)

[中文](MULTIMODAL.zh-CN.md)

First milestone of issue #2: local PNG/JPEG image input and tool-produced PNG
image output through the existing session entry points — `homebrew-harness
session` and the Python Session API. There is no new standalone entry point.

Out of scope in this milestone: audio, PDF/document and video content; native
model image/media generation; automatic media conversion; additional protocol
adapters beyond the one documented image-capable path; multimodal propagation
into Runs, Teams, Services and Workflows. All text-only behavior is unchanged.

## Ordered content-part contract

A submission is either a plain string (text-only, byte-for-byte unchanged) or
an ordered list of serializable content parts:

```json
[
  {"type": "text", "text": "Compare these charts."},
  {"type": "image", "path": "charts/first.png"},
  {"type": "text", "text": "The second one is newer:"},
  {"type": "image", "path": "charts/second.png"}
]
```

- Text part: `{"type": "text", "text": str}`.
- Image part, unfrozen: `{"type": "image", "path": str}` — a local file
  reference inside the authorized file boundary. The session freezes it at
  acceptance.
- Image part, frozen: `{"type": "image", "media": {...}}` with the descriptor
  below. Part order is preserved end to end, including provider transmission.

Frozen media descriptor (also used by tool results and the media APIs):

| Key | Meaning |
| --- | --- |
| `media_ref` | Stable content-addressed reference, `.harness/media/<sha256>.png` or `.jpg`. |
| `media_type` | `image/png` or `image/jpeg`. |
| `bytes` | Exact byte size of the frozen content. |
| `sha256` | Content identity (hex SHA-256 of the frozen bytes). |
| `width`, `height` | Pixel dimensions. |

## Image input from the CLI

In `homebrew-harness session`, attach local images inline with `[image:path]`.
Quote paths containing spaces, brackets or non-ASCII characters:

```text
> Compare these two charts. [image:"charts/销量 first.png"] [image:charts/second.jpg] Which grew faster?
```

The markup is parsed into ordered text/image parts; surrounding text keeps its
position. Malformed markup (empty path, stray `[image:`) fails with a clear
error instead of being sent as literal text. Plain-text submissions without
`[image:` are untouched.

Restrictions in this milestone: image submissions cannot bind a Goal, and
planning mode accepts text only (`/plan off` before submitting images).

### Declaring an image-capable model

Supported paths are decided jointly by the model declaration and the actual
adapter capability — never by protocol name alone. Declare input modalities in
the model profile (generic example; credentials come from environment
variables):

```json
{
  "version": 2,
  "connections": {
    "vision-http": {
      "protocol": "chat_completions",
      "api_key_env": "VISION_API_KEY",
      "base_url_env": "VISION_BASE_URL",
      "transport": "http"
    }
  },
  "profiles": {
    "vision": {
      "connection": "vision-http",
      "model_env": "VISION_MODEL",
      "capabilities": {"tools": true, "input_modalities": ["text", "image"]}
    }
  },
  "defaults": {"lead": "vision"}
}
```

`input_modalities` / `output_modalities` must be non-empty subsets of
`{"text", "image"}` and always include `text`. The verified image-input adapter
in this milestone is OpenAI-compatible Chat Completions over HTTP
(`chat_completions`); it maps ordered parts to native `image_url` data-URL
content, so the model receives real image bytes, not filenames or byte text.

Before provider submission the session validates existence, read permission,
PNG/JPEG authenticity, per-image bytes, image count, dimensions and encoded
request-body size, each with a distinct error (see below). A model or adapter
without declared image input support rejects image content with a clear
missing-capability explanation. Switching models never silently drops history
images: an incompatible selection is explained and requires a compatible model
or explicit handling — no hidden OCR or degradation. Base64 image payloads are
never counted as ordinary text context characters, and cost observation uses
provider-reported usage.

## Image input from the Python API

```python
from homebrew_harness import Harness, HostConfig

config = HostConfig.from_components(model_registry=registry)  # image-capable profile
with Harness(workdir="path/to/workspace", config=config) as host:
    session = host.sessions.open()
    turn = session.submit(
        "Compare these charts",
        parts=[
            {"type": "text", "text": "First:"},
            {"type": "image", "path": "charts/first.png"},
            {"type": "text", "text": "Second:"},
            {"type": "image", "path": "charts/second.png"},
        ],
        request_id="compare-1",
    )
    report = turn.wait(30)
    assert report.condition_met and report.snapshot.state == "completed"
    print(turn.result().text)

    for item in session.media():
        print(item["origin"], item["media_ref"], item["status"])
```

`session.execute(text, parts=...)` is the submit-and-wait convenience form.
String submissions keep working unchanged. Reusing a `request_id` deduplicates
only when the media content, order and required metadata also match.

## Frozen identity and lifecycle

At acceptance the session freezes the exact bytes content-addressed under
`.harness/media/` (mode `0600`, written through the storage SafeFile boundary)
and registers them in the artifact inventory. Later edits or deletion of the
original file never change an accepted turn. References held by live session
history and resume checkpoints are protected from garbage collection; cleanup
uses the existing retention lifecycle.

Accepted inputs and confirmed tool outputs survive follow-up turns, history
compaction (archived results keep their frozen references and an on-demand
reaccess path), save/resume and the supported backup/restore flows.
Cancellation and interruption follow the existing execution boundaries; media
preparation failure never yields a fake success, and ambiguous outcomes are
verified through the existing inspection mechanisms before any retry.

User attachments are frozen independently of the Bash output artifact policy;
tool output publication keeps the existing approval authorization.

## Tool-produced image output

The registered `render_chart` tool renders a deterministic PNG bar chart and
returns text and media in one result:

```json
{
  "text": "Rendered chart ... frozen at .harness/media/<sha256>.png ...",
  "media": [
    {"media_ref": ".harness/media/<sha256>.png", "media_type": "image/png",
     "bytes": 1234, "sha256": "<sha256>", "width": 640, "height": 480}
  ]
}
```

Arguments: `labels` (1–24 non-empty strings, ≤60 characters each), `values`
(1–24 integers, 0–1,000,000,000), optional `title` (≤80 characters), `width`
and `height` (160–2048 and 120–2048 pixels). Chart text is ASCII-only; full
labels and title remain available in the text result.

Runnable CLI example:

```text
> Render a bar chart titled "Weekly sales" with labels Mon,Tue,Wed and values 12,19,7.
> /media list
> /media show .harness/media/<sha256>.png
> /media export .harness/media/<sha256>.png exports/weekly-sales.png
```

Python API (typed references and metadata, no text scraping):

```python
for item in session.media():
    if item["origin"] == "tool_result" and item["status"] == "available":
        meta = session.media_metadata(item["media_ref"])
        session.export_media(item["media_ref"], "exports/chart.png")
```

`session.media()` returns ordered, deduplicated descriptors with `origin`
(`user_input` or `tool_result`) and retrieval `status`; `media_metadata(ref)`
describes one reference; `export_media(ref, destination)` publishes the frozen
bytes to an authorized, non-existing path.

When the selected model declares image input, tool-produced images are returned
to the model as native image content for reinspection — never as filenames or
byte text. Text-only models receive the text portion only. User attachments,
model text output and tool media results remain clearly distinguished.

## Limits

| Limit | Default | Error code |
| --- | --- | --- |
| Bytes per image | 8 MiB | `media_bytes_limit` |
| Images per request | 8 | `media_request_count_limit` |
| Total image bytes per request | 24 MiB | `media_request_bytes_limit` |
| Encoded JSON request body | 36 MiB | `media_request_body_limit` |
| Pixels per image | 40,000,000 | `media_dimensions_limit` (`image_pixels`) |
| Width/height per side | 8192 px | `media_dimensions_limit` (`image_dimension`) |
| Frozen-media read bound | 64 MiB | `media_store_corrupt` if exceeded |

Byte, count, dimension and request-body limits are reported separately, with
`limit_name`, `limit_value`, `observed_value` and `unit` details.

## Error states

| Code | Meaning |
| --- | --- |
| `media_missing` | Attachment or frozen content does not exist. |
| `media_unreadable` | Read permission denied. |
| `media_path_unauthorized` | Path outside the authorized file boundary. |
| `media_format_invalid` | Not authentic PNG/JPEG, or corrupt/non-positive dimensions. |
| `media_bytes_limit` / `media_dimensions_limit` | Per-image size/shape limit exceeded. |
| `media_request_count_limit` / `media_request_bytes_limit` / `media_request_body_limit` | Request-level limits exceeded. |
| `media_reference_invalid` | Malformed or unfrozen reference where frozen content is required. |
| `media_reclaimed` | Reference no longer resolves to stored content. |
| `media_identity_conflict` | Stored bytes do not match the recorded content hash. |
| `media_store_corrupt` | Store entry exceeds the frozen-media read bound. |
| `media_export_exists` | Export destination already exists. |
| `media_export_unauthorized` | Export destination outside the authorized boundary. |

Python API errors surface as `InvalidInput` with the code in `details["code"]`.

## Capability matrix

| Path | Model declaration | Adapter | Verification status |
| --- | --- | --- | --- |
| User image input (PNG/JPEG) | `input_modalities` includes `image` | `chat_completions` over HTTP | Verified by local protocol fixtures (real bytes, ordering, error boundaries). |
| Tool PNG reinspection by the model | `input_modalities` includes `image` | `chat_completions` over HTTP | Verified by local fixtures (native image content). |
| Tool PNG output (`render_chart`) | any | n/a (local tool) | Verified by registered tests. |
| Image input to a text-only model/adapter | no `image` | any | Rejected before provider submission with a clear explanation. |
| Other protocols / optional SDK transports | — | — | Not accepted in this milestone; no image mapping. |
| Native model image generation | — | — | Out of scope (tool-produced media only). |
| Live visual-understanding smoke | host-owned credentials + image-capable model | `chat_completions` HTTP | Not accepted in this delivery; requires host-owned credentials and capability. Local fixtures are not a substitute for live visual acceptance. |

## Tests

Registered quality coverage includes `tests/media/` (contract, store,
lifecycle), `tests/api/test_session_media.py` (CLI/API input, identity, dedup,
tool results, capability rejection), `tests/execution/test_media_adapter.py`
(native transmission and ordering) and `tests/tools/test_media_tool.py`
(renderer and publication), alongside the existing text-only regressions.
