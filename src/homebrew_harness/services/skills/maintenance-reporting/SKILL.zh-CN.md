---
name: maintenance-reporting
description: 基于工作与执行证据，为部署者汇总报告窗口中的进展、阻塞、待决定和下一步。
---

[English](SKILL.md)

按报告 ID 用 service_read 读完整窗口事实，必要时追溯证据引用和分页。合并相关变化，说明具体结果。

涵盖新增或更新需求、研判、本地完成项、失败与阻塞、待决定和下一步，保留工作引用。说明实际执行的验证与未知事项。

区分受理与执行、测试通过与审查、本地 review 与远端发布。没有已确认记录时不宣称测试、push 或 merge 成功。

通过 service_propose 提交含 text 的 JSON，运行时保存该窗口报告；此工具不会发公开回复或批准工作。
