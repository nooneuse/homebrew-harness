# 独立 Session 的图片输入与产物

[English](MULTIMODAL.md)

独立 Session 支持有顺序的 PNG/JPEG 附件和工具生成的冻结图片。原生图片输入走 **Chat Completions**，并要求宿主模型目录中的 profile 声明支持图片。其他协议、托管 Run、默认任务宿主、团队及任务 worktree 不支持图片提交。请选择普通 Lead 回合；不要在规划或 Goal 执行中附图，已有执行时可为下一轮排队，而非把图插进正在执行的 steering。

## 配置和提交

在外部模型目录中，为使用 `chat_completions` 连接的相应 profile 配置：

```json
{"capabilities": {"tools": true, "input_modalities": ["text", "image"]}}
```

供应商和具体模型还必须实际支持 `image_url`。用 `homebrew-harness session --models-config /path/to/models.json --lead-model vision` 打开独立终端 Session，其中 `vision` 为已登记的别名。路径受工作区文件边界约束，包含空格时用引号：

```text
对比旧版 [image:before.png] 和新版 [image:"after build.jpg"]。
/media list
/media show .harness/media/<sha256>.png
/media export .harness/media/<sha256>.png exports/chart.png
```

Python 可用 `session.submit("检查截图", parts=[{"type":"text","text":"检查截图"},{"type":"image","path":"screenshot.png"}])`。普通 SDK 文本仍按字面处理。`session.media()` 列出归该 Session 所有的媒体。

`render_chart` 生成 PNG，返回文本和冻结引用；`read_media` 可按当前 Session 已持有的准确引用，让支持图片的模型重新看到冻结图片。把别的 Session 的引用贴进文本不构成授权，也不会把文本伪装成图片。这里是工具生成图片，而不是模型原生出图。

## 工作集和恢复

单张上限为 8 MiB、单边 8192 像素、总计 4000 万像素；单次请求最多 8 张、24 MiB 原始图片及 36 MiB 编码请求体。尚未回答的显式附件批次必须完整容纳；旧附件和工具图像只使用剩余空间。被移出活跃请求或读取失败的历史图片以冻结引用显示，不静默删除。若新附件填满了工作集，在下一轮请求重看其他图片。

逻辑上下文估算不读取像素，也不把 base64 当成文本字符；它尚未预测供应商的视觉 token 成本。压缩后，原媒体身份独立于摘要保留：近期引用会列在摘要里，较早引用可从归档或媒体列表查到。`read_media` 读取的是原来的冻结文件，编辑或删除原附件不改变后续看到的图。保存、恢复和正常清理都保留已有的合法引用。不兼容的模型切换会被拒绝，必要时新开纯文本会话。

媒体保存在工作区私有 `.harness/media/` 中。会话、已受理附件、受信任工具记录及宿主保留引用可以保护图片不被回收；普通文本中的引用不具有这种作用。备份还需包含附件文件，仅备份数据库并不包含图片字节。

本地 fixture 可验证有序原生传输、超限恢复、压缩后重看和字节一致性。真实视觉验收需要有额度的已授权模型，提交一张非私有、具有已知视觉特征的小图，检查模型能从原生图片读出特征，再压缩后使用 `read_media` 验证一次。本地测试不消耗模型额度。
