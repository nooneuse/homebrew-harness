---
name: service-upgrade-assessment
description: 在操作者决定前评估已测试 Service 候选、兼容性、实际停靠、版本接替与记录的回滚边界。
---

[English](SKILL.md)

用 service_read 读取完整升级计划及健康、测试、运维证据，把结论绑定到候选 runtime fingerprint、安装包 hash、旧绑定和原服务身份。

检查测试源码与安装包一致、证据新鲜、写入者实际停靠，以及执行和状态契约的兼容性。仓库代码发布与运行服务升级分别处理。

说明接替、probation、确认和恢复业务的步骤及证据。测试通过后仍由操作者授权候选。

结合旧版本和当前已受理事实说明回滚。新的外部效果或业务变化需要调查；恢复旧数据库不能丢弃它们。

通过 service_propose 提交有证据的 text 评估，不批准、安装、arm 或激活候选。
