# 多模态会话（图片首期）

[English](MULTIMODAL.md)

Issue #2 的首期里程碑：通过现有会话入口 —— `homebrew-harness session` 与
Python Session API —— 支持本地 PNG/JPEG 图片输入和工具生成的 PNG 图片输出。
不新增独立命令入口。

首期不包括：音频、PDF/文档与视频；模型原生图片/媒体生成；自动媒体转换；
已验证图片适配器之外的其他协议；Run、Team、Service、Workflow 的多模态传播。
所有纯文本行为保持不变。

## 有序 content-part 合同

提交内容可以是普通字符串（纯文本，逐字节不变），或有序、可序列化的内容部件列表：

```json
[
  {"type": "text", "text": "对比这两张图表。"},
  {"type": "image", "path": "charts/first.png"},
  {"type": "text", "text": "第二张更新："},
  {"type": "image", "path": "charts/second.png"}
]
```

- 文本部件：`{"type": "text", "text": str}`。
- 未冻结图片部件：`{"type": "image", "path": str}`，指向授权文件边界内的本地
  文件，会话在接受时冻结。
- 已冻结图片部件：`{"type": "image", "media": {...}}`，携带下述描述符。
  部件顺序端到端保留，包括发送到提供商时。

冻结媒体描述符（工具结果与媒体 API 同样使用）：

| 键 | 含义 |
| --- | --- |
| `media_ref` | 稳定的内容寻址引用：`.harness/media/<sha256>.png` 或 `.jpg`。 |
| `media_type` | `image/png` 或 `image/jpeg`。 |
| `bytes` | 冻结内容的精确字节数。 |
| `sha256` | 内容身份（冻结字节的十六进制 SHA-256）。 |
| `width`、`height` | 像素尺寸。 |

## CLI 图片输入

在 `homebrew-harness session` 中用 `[image:path]` 内联附加本地图片。
含空格、方括号或非 ASCII 字符的路径请加引号：

```text
> 对比这两张图表。[image:"charts/销量 first.png"] [image:charts/second.jpg] 哪张增长更快？
```

标记被解析为有序的文本/图片部件，周围文本保持原位。畸形标记
（空路径、残缺的 `[image:`）会明确报错，而不是当作字面文本发送。
不含 `[image:` 的纯文本提交完全不受影响。

首期限制：图片提交不能绑定 Goal；规划模式只接受文本
（提交图片前先 `/plan off`）。

### 声明支持图片的模型

已支持路径由模型声明与实际适配器能力共同决定，不能仅凭协议名称推断。
在模型 profile 中声明输入模态（通用示例；凭据来自环境变量）：

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

`input_modalities` / `output_modalities` 必须是 `{"text", "image"}` 的非空子集
且始终包含 `text`。首期已验证的图片输入适配器是 HTTP 上的 OpenAI 兼容
Chat Completions（`chat_completions`）：它把有序部件映射为原生 `image_url`
data-URL 内容，模型收到的是真实图片字节，而不是文件名或字节文本。

提交到提供商之前，会话会校验文件存在性、读取权限、PNG/JPEG 真实性、
单图字节数、图片数量、尺寸与编码请求体大小，各自给出独立错误（见下文）。
未声明图片输入支持的模型或适配器会以清晰的能力缺失说明拒绝图片内容。
切换模型不会静默丢弃历史图片：不兼容的选择会被解释，需要改用兼容模型或
显式处理，不做隐蔽 OCR 或降级。Base64 图片载荷不计入普通文本上下文字符，
成本观察使用提供商实际用量。

## Python API 图片输入

```python
from homebrew_harness import Harness, HostConfig

config = HostConfig.from_components(model_registry=registry)  # 支持图片的 profile
with Harness(workdir="path/to/workspace", config=config) as host:
    session = host.sessions.open()
    turn = session.submit(
        "对比这两张图表",
        parts=[
            {"type": "text", "text": "第一张："},
            {"type": "image", "path": "charts/first.png"},
            {"type": "text", "text": "第二张："},
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

`session.execute(text, parts=...)` 是提交并等待的便捷形式。字符串提交保持
不变。重复 `request_id` 仅在媒体内容、顺序与必要元数据也一致时才去重。

## 冻结身份与生命周期

接受时，会话把精确字节按内容寻址冻结到 `.harness/media/`（权限 `0600`，
经存储 SafeFile 边界写入），并登记到产物 inventory。之后修改或删除源文件
不会改变已接受的轮次。有效会话历史与恢复检查点持有的引用受 GC 保护；
清理沿用现有保留生命周期。

已接受输入与已确认工具输出在后续轮次、历史压缩（归档结果保留冻结引用与
按需重新访问路径）、save/resume 及受支持的备份恢复中持续可用。取消与中断
沿用现有执行边界；媒体准备失败不产生假成功，结果不确定时先经现有机制
查证再重试。

用户附件的冻结独立于 Bash 输出产物策略；工具输出发布保留现有审批授权。

## 工具图片输出

注册工具 `render_chart` 渲染确定性 PNG 柱状图，在同一结果中返回文本与媒体：

```json
{
  "text": "Rendered chart ... frozen at .harness/media/<sha256>.png ...",
  "media": [
    {"media_ref": ".harness/media/<sha256>.png", "media_type": "image/png",
     "bytes": 1234, "sha256": "<sha256>", "width": 640, "height": 480}
  ]
}
```

参数：`labels`（1–24 个非空字符串，每个 ≤60 字符）、`values`（1–24 个整数，
0–1,000,000,000）、可选 `title`（≤80 字符）、`width` 与 `height`
（分别 160–2048 与 120–2048 像素）。图内文字仅 ASCII；完整标签与标题保留在
文本结果中。

可运行的 CLI 示例：

```text
> 渲染标题为 "Weekly sales" 的柱状图，标签 Mon,Tue,Wed，数值 12,19,7。
> /media list
> /media show .harness/media/<sha256>.png
> /media export .harness/media/<sha256>.png exports/weekly-sales.png
```

Python API（类型化引用与元数据，无需刮取文本）：

```python
for item in session.media():
    if item["origin"] == "tool_result" and item["status"] == "available":
        meta = session.media_metadata(item["media_ref"])
        session.export_media(item["media_ref"], "exports/chart.png")
```

`session.media()` 返回有序、去重的描述符，含 `origin`（`user_input` 或
`tool_result`）与获取 `status`；`media_metadata(ref)` 描述单个引用；
`export_media(ref, destination)` 把冻结字节发布到授权范围内且尚不存在的路径。

当所选模型声明图片输入时，工具图片以原生图片内容返回给模型复检，绝不是
文件名或字节文本；纯文本模型只收到文本部分。用户附件、模型文本输出与
工具媒体结果始终明确区分。

## 限制

| 限制 | 默认值 | 错误码 |
| --- | --- | --- |
| 单图字节数 | 8 MiB | `media_bytes_limit` |
| 每请求图片数 | 8 | `media_request_count_limit` |
| 每请求图片总字节 | 24 MiB | `media_request_bytes_limit` |
| 编码 JSON 请求体 | 36 MiB | `media_request_body_limit` |
| 单图像素数 | 40,000,000 | `media_dimensions_limit`（`image_pixels`） |
| 单边宽/高 | 8192 px | `media_dimensions_limit`（`image_dimension`） |
| 冻结媒体读取上限 | 64 MiB | 超出报 `media_store_corrupt` |

字节、数量、尺寸与请求体限制分别报告，附 `limit_name`、`limit_value`、
`observed_value` 与 `unit` 详情。

## 错误状态

| 错误码 | 含义 |
| --- | --- |
| `media_missing` | 附件或冻结内容不存在。 |
| `media_unreadable` | 无读取权限。 |
| `media_path_unauthorized` | 路径超出授权文件边界。 |
| `media_format_invalid` | 非真实 PNG/JPEG，或损坏/非正尺寸。 |
| `media_bytes_limit` / `media_dimensions_limit` | 单图大小/形状超限。 |
| `media_request_count_limit` / `media_request_bytes_limit` / `media_request_body_limit` | 请求级限制超限。 |
| `media_reference_invalid` | 引用畸形，或需要冻结内容时仍未冻结。 |
| `media_reclaimed` | 引用已无法解析到存储内容。 |
| `media_identity_conflict` | 存储字节与记录的内容哈希不一致。 |
| `media_store_corrupt` | 存储条目超出冻结媒体读取上限。 |
| `media_export_exists` | 导出目标已存在。 |
| `media_export_unauthorized` | 导出目标超出授权边界。 |

Python API 错误以 `InvalidInput` 呈现，错误码位于 `details["code"]`。

## 能力矩阵

| 路径 | 模型声明 | 适配器 | 验证状态 |
| --- | --- | --- | --- |
| 用户图片输入（PNG/JPEG） | `input_modalities` 含 `image` | HTTP `chat_completions` | 本地协议 fixture 已验证（真实字节、顺序、错误边界）。 |
| 模型复检工具 PNG | `input_modalities` 含 `image` | HTTP `chat_completions` | 本地 fixture 已验证（原生图片内容）。 |
| 工具 PNG 输出（`render_chart`） | 任意 | 不适用（本地工具） | 注册测试已验证。 |
| 向纯文本模型/适配器输入图片 | 无 `image` | 任意 | 提交提供商前明确拒绝并解释。 |
| 其他协议 / 可选 SDK 传输 | — | — | 首期未验收；无图片映射。 |
| 模型原生图片生成 | — | — | 不在范围（仅工具媒体）。 |
| 真实视觉理解 smoke | 宿主自有凭据 + 支持图片的模型 | HTTP `chat_completions` | 本次交付未验收；需宿主自有凭据与能力。本地 fixture 不能替代真实视觉验收。 |

## 测试

注册 quality 覆盖包括 `tests/media/`（合同、存储、生命周期）、
`tests/api/test_session_media.py`（CLI/API 输入、身份、去重、工具结果、
能力拒绝）、`tests/execution/test_media_adapter.py`（原生传输与顺序）、
`tests/tools/test_media_tool.py`（渲染与发布），以及既有纯文本回归。
