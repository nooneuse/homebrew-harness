# Service 运维

[English](SERVICE_OPERATIONS.md)

Service 运维提供健康事实、写入者停靠、跨工作区保留引用、备份集合、仓库审查和兼容版本升级。它复用 Service、Run、TestRunner 和现有存储控制器；长操作返回持久编号，CLI、Text UI 与 Python 共用认证控制 API。

## 宿主配置

在仓库外 ServiceSpec 中增加 `operations`，完整示例见[英文文档](SERVICE_OPERATIONS.md#host-policy)。主要设置包括固定 Git 程序、外部备份目录、操作期限、磁盘阈值、审查范围/模型、保留参数及升级候选目录。

备份目录需仅属主可访问，并位于项目和控制目录之外。升级候选的验证源码和安装 package 同样使用外部宿主目录。`mode="isolated"` 要求候选运行版本测试使用真实 ContainerBackend；显式 local 模式用于可信、无发布凭据的原生测试场景。费用继续由提供商管理。

固定调度支持 `action="review_repository"`，例如 `every_seconds=43200` 表示每半天审查。该动作要求 repository_maintenance profile。模型不能指定执行程序、连接地址或任意清理目录。

## 健康与操作记录

```sh
homebrew-harness service ops SERVICE_ID health
homebrew-harness service ops SERVICE_ID list
homebrew-harness service ops SERVICE_ID show OPERATION_ID
homebrew-harness service ops SERVICE_ID read OPERATION_ID --offset 0 --limit 4000
```

健康区分存活、执行就绪、等待操作者和进展事实，提供 worker 错误、来源采集位置、工作/请求/动作计数、心跳时龄、磁盘空间，以及未确定交付或工作区同步。健康查询不调用模型。

异常保留身份和解决状态。可选诊断使用既有模型接入和 service-incident-triage Skill，产生解释及复验建议。模型文字不会授权恢复。

操作使用 queued、running、completed、blocked 状态和稳定 request_id。准备操作的完成可能只表示已产生独立审查任务，需要继续查询对应 job/audit。列表提供摘要，长计划通过分页记录读取。

## 停靠与恢复业务

```sh
homebrew-harness service ops SERVICE_ID park --request-id maintenance-1
homebrew-harness service ops SERVICE_ID resume --request-id resume-1
```

停靠停止新的业务执行，关闭来源/交付 worker，并暂停及收尾所属原生 Run，保留 HTTP 控制服务。draining 表示资源仍在结束；parked 确认本地写入边界。普通业务变更需要显式恢复后受理，查询和宿主运维仍可使用。

停靠和重启保留原生工作身份及待决定请求。恢复先检查运行版本、模型和 Skill 绑定，再重建业务 worker。任意 Python 扩展线程仍需合作式取消，未结束时继续保留归属。`stop` 则关闭控制服务及整个 Service。

## 保留、备份和恢复

```sh
homebrew-harness service ops SERVICE_ID retention-preview --request-id retention-1
homebrew-harness service ops SERVICE_ID retention-apply PLAN_OPERATION_ID --request-id prune-1
homebrew-harness service ops SERVICE_ID backup --request-id backup-1
homebrew-harness service ops SERVICE_ID verify-backup BACKUP_ID --request-id verify-1
homebrew-harness service ops SERVICE_ID restore-backup BACKUP_ID --request-id restore-1
```

未结束工作、适用决定、未读报告、外部结果未知和待工作区同步会保护相应会话与证据。关闭 Service 不解除这些引用。checkpoint 退休沿用“完成收尾超过 15 天，且不在最近 10 个会话中”的政策。旧的无引用候选可以释放重产物，工作、提交身份和确认交付事实仍可查询。

预览与执行绑定业务事实、保留根和文件版本；执行时重新检查并记录进度。宿主显式应用计划，模型不能选择任意目录进行删除。

备份集合包含控制库、所属执行库及必要附件的 SQLite 一致副本和完整 hash 清单，排除连接凭据、HTTP token/endpoint、锁和外部宿主文件。备份验证可以在业务运行时执行，创建和恢复则要求实际停靠并取得执行边界。

恢复保留当前运维回执和控制连接，通过 journal 记录跨工作区进度。首版恢复原目录关系，要求业务事实未变化；后来受理的新工作、输入或交付不会被旧备份覆盖。无法确认当前事实的灾难恢复和跨主机迁移由宿主结合完整备份进行明确调查。

## 周期审查与持续开发

```sh
homebrew-harness service ops SERVICE_ID audit --request-id review-1
homebrew-harness service ops SERVICE_ID audit --force --request-id full-review-1
```

审查固定工作快照及观察到的 Git HEAD，在独立模型上下文中复用 code-review，提交结构化发现。发现进入已有需求研判和授权流程，重复的未解决发现关联原工作。相同内容和范围复用已成功审查，force 可以显式重查。HEAD 未变化但工作内容发生变化时仍需新证据。

每轮普通开发使用独立审查分支，CI 修复继续使用本轮分支和变更请求。工作新版本重新捕获输入基线。CI 修复交付后，仅在其他写入者停止且原内容符合前置条件时把变更回写主工作区；发生冲突时保留新改动并在健康/工作状态中显示。

## 兼容版本升级

在 operations.upgrade_candidates 中登记候选 alias，包括外部 `workspace`、已安装 `package`、固定 `recipe` 和 `version`。验证源码使用项目的 src/homebrew_harness 布局，测试配方需要覆盖所有安装 Python 和 Skill 文件。准备时检查源码与安装包一致、测试证据新鲜；isolated 模式使用 ContainerBackend，候选测试不接收发布凭据。

```sh
homebrew-harness service ops SERVICE_ID upgrade-prepare candidate --request-id upgrade-1
homebrew-harness service ops SERVICE_ID upgrade-arm PLAN_ID --request-id authorize-1
homebrew-harness service stop SERVICE_ID
```

宿主安装或选择批准的版本，用该环境启动已保存的服务。允许接替的绑定必须与计划完全匹配，新实例保持停靠并进入 probation：

```sh
homebrew-harness service ops SERVICE_ID upgrade-confirm PLAN_ID --request-id confirm-1
homebrew-harness service ops SERVICE_ID resume --request-id resume-new-1
```

首版保留当前状态 schema 和执行配置合同；解释器及测试命令身份需要与已保存执行绑定兼容。版本及 Skill 绑定显式更新，安装和进程切换由宿主完成。service-upgrade-assessment Skill 在授权前解释证据和兼容边界。

未确认接替且业务事实未变化时，进程结束后可通过明确的宿主维护入口回滚：

```sh
homebrew-harness service ops SERVICE_ID upgrade-rollback PLAN_ID --request-id rollback-1 --cwd CONTROL_DIR
```

之后启动记录的旧安装版本，它仍保持停靠。已确认升级或产生新业务效果的情况需要新的恢复决定，保留已有 journal 和回执。

## Python 与演示

Service 视图提供 health、operations/operation/operation_record、quiesce/resume_operations、audit、diagnose、retention、backup/verify/restore、upgrade prepare/arm/confirm。AsyncHarness 提供对应可等待方法；离线回滚使用 `host.admin.service_upgrade_rollback` 和稳定 request_id。

```sh
python examples/service_operations_demo.py --output /tmp/harness-service-operations-demo
```

使用不存在的输出目录。场景扩展真实本地 Git/Run/CI 交付，验证第二轮审查开发、无变化跳过、持久讨论、HTTP 停靠、保留、备份验证/恢复及升级准备、授权和回滚。实际新版本进程的 probation、原生执行和回滚另有进程测试。确定性模型/平台服务不调用付费模型。

仓库审查在写入者活跃时保留排队意图，claim 后才开始执行时限；重复周期合并为一个待审查操作。测试清理使用独立、有界的尝试，资源未释放时保留租约与待收尾状态，后续尝试确认后才完成关闭。
