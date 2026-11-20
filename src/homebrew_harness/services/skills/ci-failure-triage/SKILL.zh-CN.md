---
name: ci-failure-triage
description: 核对已发布候选 SHA 的检查证据，为 CI 失败提出有界修复或部署者操作建议。
---

[English](SKILL.md)

读取 ChangeSet 的本地质量或远端 CI 证据，确认属于当前固定候选。本地测试的 `quality[].outputs` 提供精确 ID，可通过 `service_read(kind="quality_output", identity=...)` 分页读取日志；用 service_read_code 查看固定候选代码。区分代码、基线、测试不稳定、基础设施和不明失败。仅局部重跑通过不能证明负载抖动。

先看输入中的 `work_context` 和 `validation_history`，利用当前执行说明、已接受范围、开发交接和历史失败回执。公开框架修复可能需要集成进冻结候选，升级运行包本身不会修改候选。通过 `service_read(kind="changesets", identity=job.target)` 读取同一证据投影，`work` 读取绑定上下文。代码 offset 是字符位置；分页沿用返回的 `next_offset` 和 `expected_version`。获得足够证据后提交研判和有界计划，保留已完成工作。

仅对已确认代码缺陷提出最小修复，保留原任务、允许路径、固定测试和授权期限，不删除测试、扩大权限或用新 Run 清零失败次数。

通过 service_propose 返回 classification（code/baseline/flaky/infrastructure/unknown）、reason、plan。本地 flaky/infrastructure 分类请求同一候选的有界重测，说明依据和预期恢复；baseline/unknown 分类说明所需部署者动作。提议不会豁免测试或批准交付。
