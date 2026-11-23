# Images in standalone Sessions

[简体中文](MULTIMODAL.zh-CN.md)

A standalone Session accepts ordered PNG/JPEG attachments and can return frozen images made by tools. The documented native-input route is **Chat Completions**, with an image-capable model selected in the host model catalog. Other protocols, managed Runs, the automatic task host, teams and task worktrees do not enable image submission. Start a normal Lead turn outside planning or Goal execution; enqueue a new turn instead of attaching images to an in-flight steering message.

## Configure and submit

Add this declaration to a tool-capable profile backed by a `chat_completions` connection in your external model catalog. Use a provider/model that actually accepts `image_url` inputs. Configuration is not a live capability test.

```json
{"capabilities": {"tools": true, "input_modalities": ["text", "image"]}}
```

Open a standalone terminal Session with `homebrew-harness session --models-config /path/to/models.json --lead-model vision`, where `vision` is the configured alias. Local attachment paths use the normal workspace file boundary. Quote paths containing spaces:

```text
Compare the old [image:before.png] and the new [image:"after build.jpg"].
/media list
/media show .harness/media/<sha256>.png
/media export .harness/media/<sha256>.png exports/chart.png
```

The Python Session API also accepts ordered `parts` (text remains literal unless explicitly supplied as parts):

```python
from homebrew_harness import Harness, HostConfig

config = HostConfig.from_files(models_config="/path/to/models.json", lead_model="vision")
with Harness(workdir="/path/to/project", config=config) as host:
    session = host.sessions.open()
    turn = session.submit("Inspect the screenshot", parts=[
        {"type": "text", "text": "Inspect the screenshot"},
        {"type": "image", "path": "screenshot.png"},
    ])
    print(turn.wait(60).snapshot.state)
    for item in session.media():
        print(item["media_ref"], item["status"])
```

`render_chart` produces a PNG and returns its reference and text. `read_media` takes an exact reference held by the current Session and returns the original frozen image to an image-capable model. The output is tool-produced, not native model image generation. A reference copied from another Session or merely pasted into unrelated text does not confer ownership.

## Working set, compaction and recovery

The per-image limit is 8 MiB, with 8192 pixels per side and 40 million pixels total. The per-request working set is at most 8 images, 24 MiB of raw media and 36 MiB for the encoded request. An explicit, unanswered attachment batch must fit and is not silently reduced. Recent history and tool images use the remaining space; omitted or unavailable historical images are shown as frozen references. If a new batch fills the entire visual window, make a separate turn for additional re-reads.

Context sizing uses a logical request and does not load or count base64 bytes as text. This is not a provider-specific image-token estimate. Text context and wire limits remain separate. `/compact` archives older history and keeps the original media identities independently of the summary. The newest references are listed in the compacted context; earlier ones can be found in the retained transcript or media listing. Use `read_media` to re-send the frozen bytes even if the original attachment was edited or removed. Save/resume preserves those references. A model switch that cannot carry held images is rejected; start a new text conversation if needed.

Frozen bytes live under the Session workspace's private `.harness/media/` store. No other tool path grants permission to read it. References held in durable Session history, accepted attachments and trusted tool records protect them from normal GC, together with host retention pins. A backup must include the workspace attachments; a database-only backup does not include image bytes. For changes to private deployment files, use the usual operator-owned configuration process.

Local fixtures verify ordered native requests, image-window recovery, re-reading exact frozen data and retention. A real image-capable model smoke test is separate: use an authorized provider, attach a small nonprivate image with a known visual feature, ask about the feature, compact, and check an explicit `read_media` re-read. No live provider is needed for the local fixtures.
