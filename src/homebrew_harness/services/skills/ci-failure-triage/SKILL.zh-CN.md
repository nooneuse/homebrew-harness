---
name: ci-failure-triage
description: 核对已发布候选 SHA 的检查证据，为 CI 失败提出有界修复或部署者操作建议。
---

[English](SKILL.md)

读取 ChangeSet 完整 CI 证据并确认 head 匹配候选，用 service_read_code 查看固定候选代码。区分代码、基线、测试不稳定、基础设施和不明失败。

仅对已确认代码缺陷提出最小修复，保留原任务、允许路径、固定测试和授权期限，不删除测试、扩大权限或用新 Run 清零失败次数。

通过 service_propose 返回 classification（code/baseline/flaky/infrastructure/unknown）、reason、plan；其他失败说明所需观察或部署者动作。提议不启动修复，也不批准交付。
