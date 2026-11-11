---
name: delivery-planning
description: 依据固定候选与证据解释交付计划、变更请求说明和逐项审批范围。
---

[English](SKILL.md)

用 service_read 读取当前 ChangeSet、候选、质量证据和交付动作，说明具体工作版本、commit/tree、开发分支、目标基线及待决定。

区分本地就绪与当前远端 CI/交付就绪，仅建议宿主登记的目标和操作。身份、凭据、程序和连接配置归宿主管理。

依据确认的改动与测试写说明，使用关联引用，不引入自动关闭 Issue 的关键字。说明警告和基线变化，再提出交付建议。

分组展示仍保留每个请求引用与版本，决定由部署者逐项完成。普通 approved/yes 文字不批准；外部结果未知时申请检查，不建议重新写入。

说明 PR 优先交付：manual 等待平台维护者，service_approved 只建议已登记的固定 head 计划。PR Review Approve 与服务合并授权分别管理；依据回执说明真实 commit／内容证明、待核验及同步。不要建议直接推主分支。可选 publication 正文需要具体操作者决定，不发布内部备注。
