# 持久服务

当前预算默认值与迁移见[运行预算](LIMITS.zh-CN.md)。

[English](SERVICES.md)

Service 管理持久工作队列与固定周期计划，每项工作交给已有 Run supervisor。P1 提供 `manual` profile：部署者提交任务、观察进展、处理原生请求，并暂停或停止执行。

`repository_maintenance` profile 增加 GitHub 收件、自动研判、工作授权、长期对话和汇报。工作项在委派 Run 完成后进入 `review`。可选交付能力提供固定候选验证、分组发布审批与远端交付；社区沟通能力提供 Issue 多轮讨论和外部 PR 审查。

启用 Delivery 时，开发 Run 完成实现、授权范围内的清理和注册验证，再报告结果及剩余验收限制。Service 随后准备并审查候选，按已配置的审批策略发布分支和 PR。开发 Run 在 PR 创建前完成，其代码工具无需平台凭据；身份、公开正文前缀和合并责任约定在交接中继续生效。

Service Run 提供 `get_work_progress` 和 `submit_work_progress`，在适当边界保存摘要和剩余工作；开发完成后单独提交 `complete=true`。已核对的源码快照和交接保存在 Work 上，换 Run 后仍可继续。恢复已有交接或 ChangeSet 时进入其当前阶段，不重新开启开发；普通成功的 Run 完成也使用同一交接合同。

Delivery 对冻结候选进行最终验证。相同声明测试输入和容器环境下的稳定证据可以跨 Run 引用；`run_tests` 始终真实执行。复用会标明原始回执和执行时间。输入或合同变化、局部测试及更新的相关失败要求重新验证；外部观察及未完整声明的原生环境默认不复用。

候选准备保留原始工作输入和 Git 基线，在隔离仓库中与观察到的目标提交整合。已有输入草稿必须明确提出纳入候选；已经发布的修改不重复加入差异，冲突交给受限修复流程。目标已有经审查的内容时会如实报告，不伪造 PR 或合并回执。

## 宿主准备

默认交互中的内置 host-project Skill 可以引导配置，详见[托管仓库](HOSTING.zh-CN.md)。启动前通过 `service validate --spec ... --host-config ... --cwd ...` 进行离线配置检查。

控制状态与执行工作区使用独立目录。控制目录仅允许 owner 访问；运行时安装在目标源码之外，维护 harness 自身时也采用这一布局。

```sh
mkdir -m 700 /path/to/private-control
homebrew-harness service --cwd /path/to/private-control start \
  --spec /path/to/private/service.json \
  --host-config /path/to/private/host.json --detach
```

宿主配置采用现有 InteractionConfig 字段，包含模型／工具文件引用、能力目录、模型别名和执行默认值；环境文件也可通过 `--env-file` 选择。配置文件位于两个工作区之外。

服务声明示例：

```json
{
  "version": 1,
  "profile": "manual",
  "workspace": "/path/to/project",
  "run_defaults": {
    "total_seconds": 1800,
    "episode_model_calls": 12,
    "write_paths": ["src", "tests"]
  },
  "schedules": [
    {"name": "checkpoint", "every_seconds": 43200, "action": "record_tick"}
  ]
}
```

未指定的 Run 默认值继承宿主设置，显式 run_defaults 使用现有 RunSpec 契约，继续校验模型、测试、路径、阶段和权限。

`record_tick` 保存周期触发，不调用模型；`submit_work` 还需 `prompt`，将固定宿主任务加入队列。重启错过的多个周期合并为一次补触发，前一周期任务未完成时不重复排队。触发受理与任务完成分别记录。

P1 使用一个串行执行工作区。非终态或未收尾 Run 保留原工作区限制；服务等待外部已有执行，不停止不属于自己的 Run。

## 观察与提交

启动结果给出 `svc_...` 服务 ID：

```sh
homebrew-harness service --cwd /path/to/private-control status SERVICE_ID
homebrew-harness service --cwd /path/to/private-control attach SERVICE_ID
homebrew-harness service --cwd /path/to/private-control work SERVICE_ID submit \
  "实现本地改动" --request-id feature-1
homebrew-harness service --cwd /path/to/private-control work SERVICE_ID list
homebrew-harness service --cwd /path/to/private-control requests SERVICE_ID
```

Text UI 使用相同操作，省略服务 ID：

```text
service> status
service> work submit "实现本地改动" --request-id feature-1
service> work list
service> work show WORK_ID
service> focus WORK_ID
service> requests
service> answer REQUEST_ID REVISION "保持当前行为" --decision-id answer-1
service> approve REQUEST_ID REVISION --decision-id approve-1
service> pause execution
service> resume
service> exit
```

未识别为固定命令的文本作为普通工作提交。`focus WORK_ID` 选择工作和输入版本，普通文本成为该执行的 steering；`focus` 清除目标。焦点按控制会话保存，重新连接可恢复；尚无原生执行的工作会提示当前条件。文本不解释为批准。`work steer/interrupt WORK_ID TEXT` 绑定相应工作；`work pause/resume/cancel WORK_ID` 发送明确控制。

`approve/deny` 处理工具请求，`answer` 回答需求问题，`renew-tests` 使用 `--runs/--seconds` 续期固定测试授权。请求保留原 target、revision、参数和 expiry，遇过期引用应刷新。P1 尚无分组审批。

外部宿主配置可启用[自动审批审核](APPROVALS.zh-CN.md)。原生 Run 审核已委托的工具请求；明确配置业务种类后，还可独立审核 Service 工作、交付或沟通请求。不确定的请求继续使用同一人工控制入口。

Service 审批使用当前已接受的 Work 需求与执行说明、冻结动作、候选身份及已记录的审查和验证事实。它与研判任务共用 Work 证据投影，并通过候选证据导航定位仓库内容。

等待中的变更请求会跟进目标分支变化。无冲突的集成重新进入验证与审查；存在冲突时，将固定的私有候选和冲突路径交给现有研判任务。此前的审查和验证保留为历史事实。修复仍遵循授权、限额与准确发布审批，更新沿用同一个变更请求。

交付审批接收一份准确动作计划，请求与发布记录中的同值副本引用该计划。审核每轮报告剩余输入空间，模型据此选择相关证据与读取页大小。

`exit` 或 EOF 断开界面，detached 服务继续运行；Ctrl-C 取消当前输入／观察；`stop` 才请求停止服务。界面采用普通 JSON／文本，外部终端控制字符转义，不依赖 UI 库。Text UI 和单项控制命令使用同一套认证 HTTP API。

## HTTP 连接与远程客户端

提交输出通过工作 ID 和 request_id 确认受理，执行进度用 status/show 查询。传输错误也显示 request_id，便于在回执丢失后检查原请求。

服务默认只绑定回环地址，可在 ServiceSpec 中设置 control_port，默认由系统分配。服务生成独立控制令牌，每次实例启动写入私有 `.harness/control/endpoint.json`。本地控制命令只读取连接配置，不查询控制数据库。

远程连接文件位于仓库外：

```json
{
  "version": 1,
  "service_id": "SERVICE_ID",
  "endpoint": "CONTROL_ENDPOINT",
  "token_env": "HARNESS_CONTROL_TOKEN"
}
```

在私有文件填写实际 ID/地址，也可以用 token_file 引用包含 version、service_id、token 的凭据 JSON。只选择一种令牌来源；本地发现还绑定实例。凭据与连接值不进入正式仓库、命令行令牌参数或日志。

```sh
homebrew-harness service --connection /path/to/private/client.json status SERVICE_ID
homebrew-harness service --connection /path/to/private/client.json attach SERVICE_ID
```

远程使用 SSH 转发到回环服务；明文 HTTP 仅接受回环地址，明确部署的远端入口使用 HTTPS。客户端保留证书校验，禁用环境代理、重定向和隐式命令重试。

接口为 POST `/v1/control`（登记查询与命令）、GET `/v1/services/{id}`（快照）、认证后 GET `/v1/health`。JSON 请求包含 version、service_id、method、payload、request_id。所有 API 使用 Bearer 认证，检查 Host/Origin，不开放 CORS；未来 Web UI 可按同源方式复用这些接口。

HTTP／JSON 大小、连接和读取时间有界，长内容按引用／分页查询。超时和响应丢失不证明命令未受理，应保留原 ID，查询回执或重放同 ID/内容。

stop 后服务可能不可达，远程客户端显示连接未知，不猜测 stopped。宿主可用 `service inspect SERVICE_ID --cwd CONTROL_DIR` 显式检查保存状态；RemoteService 和 Text UI 不在网络失败后退回本地数据库。

## 暂停、停止与重启

```sh
homebrew-harness service --cwd /path/to/private-control pause SERVICE_ID --scope execution
homebrew-harness service --cwd /path/to/private-control resume SERVICE_ID
homebrew-harness service --cwd /path/to/private-control stop SERVICE_ID
homebrew-harness service --cwd /path/to/private-control start SERVICE_ID --detach
```

暂停阻止新工作并请求原生安全暂停。暂停时提交的任务保留在队列；暂停尚未执行的队列项不阻挡其他就绪项，但暂停的 Run 仍占用工作区。

停止会暂停所属活动 Run，并等待真实资源。state、resources_closed、live_owner 与原生执行状态分别表达事实；待收尾状态保留归属。原生清理需要检查时，使用已有 Run recovery 操作，再重试关闭。

重启读取保存的宿主配置并校验运行时／执行绑定，保留工作 ID、Run ID、请求和调度位置。因服务停止而暂停的执行可接续；独立暂停或阻塞的 Run 通过相应 work 控制恢复。

未确认启动显示 startup_pending。检查保存 token 后，可用 `recover-launch SERVICE_ID --token TOKEN` 在取得真实租约后隔离旧 token；延迟的旧子进程无法再使用它。确认旧 supervisor 已死亡时可以重建服务，原生 worker 恢复仍遵循 Run 的 fencing。

## Python API

```python
from homebrew_harness import Harness, HostConfig, ServiceSpec, ScheduleSpec

with Harness(workdir=control_dir, config=HostConfig.from_files(env_file=env_file)) as host:
    service = host.services.start(
        ServiceSpec(str(project_dir), schedules=(ScheduleSpec("checkpoint", 43200),)),
        lifetime="detached",
        request_id="maintenance",
    )
    work = service.work.submit("实现本地改动", request_id="feature-1")
    print(work.wait(120).data())
```

`host.services.get(id)` 只观察，发送控制使用 `control=True`。当前进程也可采用 attached 模式，关闭 Harness 会停止其所属服务；detached 服务由独立 supervisor 管理。

`service.work.list(state=..., cursor=..., limit=...)` 分页返回摘要；`work.input(offset=..., limit=...)` 分块读取原始任务。`work.snapshot()` 投影原生执行，`work.result()` 读取委派执行的确认结果；review 是可处理状态。

`host.requests.list(target=service.ref)` 和 `host.requests.answer/approve/...` 将请求交给实际 Run 控制器。`service.receipt(request_id)` 查询可能丢失返回的控制回执；相同 ID 配不同命令内容会被拒绝。

AsyncHarness 的 service 与嵌套 work 方法同样可等待：

```python
service = await host.services.start(spec)
work = await service.work.submit("实现改动", request_id="feature-1")
report = await work.wait(120)
```

持久服务依赖可重建的宿主文件，P1 不将进程内客户端、Hook 或 Workflow callable 序列化到 supervisor。

独立程序无需本地控制目录：

```python
from homebrew_harness import RemoteService

with RemoteService.from_file(connection_file, control=True) as service:
    work = service.work.submit("实现本地改动", request_id="feature-1")
    print(work.wait(120).data())
```

RemoteService／ControlConnection 与共用契约、观察层不加载 POSIX 执行后端，可作为 Windows 上的控制客户端。原生 Windows 的文件、租约和进程执行适配仍是后续工作。

关闭 RemoteService 会释放它拥有的 HTTP 连接；传入已有 ControlClient 时默认借用，设置 `own_client=True` 可移交关闭责任。本地观察者 Harness 关闭时也会释放 HTTP 连接，外部 Service 继续运行。

## 部署与演示

```sh
python examples/service_demo.py --output /tmp/harness-service-demo
```

演示使用确定性本地服务，不调用付费模型。系统级开机启动／重启由 systemd、launchd 或既有容器宿主负责；服务记录执行归属和暂停／停止意图。

控制入口采用认证 HTTP，消息、连接和方法均有边界，只开放登记操作。标准库服务是受限的本地控制端点，公网 Web 部署与浏览器会话认证需要单独验收。服务队列和调度使用当前 SQLite 后端，保持绑定、期限和真实收尾，费用由提供商管理。

## 仓库维护（P2）

仓库收件通过明确的 provider/instance/project 选择；当前适配器实现 GitHub，通用工作和授权不依赖平台。配置、对象身份和扩展契约见 [代码管理平台](PLATFORMS.zh-CN.md)。

`repository_maintenance` profile 提供仓库收件、需求研判、工作授权、持久对话与报告。GitHub 写入与发布仍由后续交付能力处理。模型提出 scope 和计划，宿主验证并保存授权后才启动原生 Run。

外部 ServiceSpec 可增加以下配置（仍需提供 version、workspace 和 run_defaults）：

```json
{
  "profile": "repository_maintenance",
  "sources": [{
    "name": "project",
    "provider": "github",
    "instance": "primary",
    "project": "OWNER/REPOSITORY",
    "api_url_env": "PROJECT_API_ENDPOINT",
    "token_env": "PROJECT_READ_TOKEN",
    "poll_seconds": 43200,
    "max_pages": 5
  }],
  "maintenance": {
    "objectives": "维护已有行为和兼容性。",
    "non_goals": "不发布、不增加未支持的平台。",
    "scopes": {
      "code": {
        "write_paths": ["src", "tests"],
        "tools": ["read_file", "write_file", "edit_file", "delete_file", "glob", "search_text"],
        "allow_tests": []
      }
    },
    "auto_categories": ["bug", "documentation"],
    "authority_seconds": 3600,
    "report_seconds": 43200
  }
}
```

连接值只放外部宿主文件——既可放在宿主环境，也可在单个 source 上配置可选的外部仅属主 `credentials_file`（仅从该文件取值，不回退宿主环境）。Source 凭据由固定只读适配器使用。scope 的路径、工具、固定测试必须属于 Run 默认范围；未配置 auto_categories 时，可执行提议交给部署者批准。自动类别只选择预授权范围，不扩大权限，也不跳过原生工具审批。

轮询保存 Issue/PR 和评论版本、分页位置与去重记录。轮询只能观察尚可读取的版本，不能恢复两次轮询间消失的编辑或删除。失败保留游标并显示重试状态，无变化时不调用模型。

三份内置 Skill：requirement-triage、maintenance-planning、maintenance-reporting 按需加载正文。服务模型可以读记录和可见代码并提交候选，不能使用 Bash、批准或发布。实际范围与版本由宿主验证。

可选 maintenance.instructions 按任务保存有界文字偏好，支持 common、development、triage、discussion、review、ci_triage、validation_plan、publication、audit、conversation、report、incident、upgrade_assessment、recovery_plan 和 integration_assessment。推理任务只收到 common 与对应条目；编译的开发 Run 收到 common 与 development，并保留原始需求和范围。未知任务名、空／非文本条目、私有连接值、单条超过 8,000 字符或合计超过 16,000 字符会被拒绝。指引影响模型行为，模型选择、路径、测试和授权仍使用结构化合同。详见[定制部署](HOSTING.zh-CN.md#定制部署)。

```sh
homebrew-harness service records SERVICE_ID events
homebrew-harness service records SERVICE_ID sources
homebrew-harness service records SERVICE_ID jobs
homebrew-harness service requests SERVICE_ID
homebrew-harness service approve SERVICE_ID REQUEST_ID REVISION --decision-id decision-1
homebrew-harness service chat SERVICE_ID "讨论下一阶段的优先级" --request-id discussion-1
homebrew-harness service message SERVICE_ID MESSAGE_ID
homebrew-harness service report SERVICE_ID
homebrew-harness service records SERVICE_ID reports
homebrew-harness service report SERVICE_ID REPORT_ID
homebrew-harness service report-ack SERVICE_ID REPORT_ID
```

这些命令可配合外部 `--connection` 文件；`record KIND ID --offset ... --limit ...` 分块读长记录。`work revise WORK_ID TEXT` 修订非活动工作的契约，`work revoke WORK_ID` 撤销工作授权；活动执行继续使用明确的 steering、pause、cancel 和恢复控制。续期授权与恢复执行是两个明确操作。

此 profile 的 Text UI 普通输入进入长期对话；`work submit` 请求新工作，`work steer` 修改活动 Run。焦点用于绑定讨论目标，候选计划受理时仍需匹配需求版本，模型回复不会成为批准。

每类维护任务使用统一的输入和结果契约。对话可读取截至当前消息的同一 context 历史，以及明确选定 Work 的关联证据；通用对话可浏览最小 Work 目录。报告读取冻结活动窗口与引用记录，并区分历史事实和当前详情。模型提交候选后，运行时校验并将业务结果与 job 完成状态放在同一事务中提交。

Python 对应 `service.converse()`、`service.conversation(id)`、`service.records(kind)`、`service.report()` 等方法。消息及回复可在重连后查询，报告引用工作与执行事实；ack 保存当前读者的已读位置。P2 通过控制接口提供报告，不发送公开 GitHub 回复，也不执行分组发布审批。

`reports` 查询当前控制 context 的未读就绪报告。`job-retry JOB_ID --request-id ...` 明确授权另一个有界研判尝试周期，累计尝试数继续保留。`work request-authority WORK_ID` 为当前计划刷新过期或被拒请求，不重新运行研判，也不直接授予执行权限。信息请求通过 `answer` 回答，答复修订需求后再次研判。

完整确定性 P2 演示：

```sh
python examples/maintenance_demo.py --output /tmp/harness-maintenance-demo
```

演示轮询本地 GitHub fixture，自动处理范围内的 Bug，跨服务重启保留功能请求的决定，实际执行固定测试，恢复讨论并生成报告，不调用付费模型。

工作快照提供 verification 引用，通过 `evidence` 记录可查原生测试凭证与所属需求版本。执行中来源发生更新时，旧 Run 的完成结果作为历史证据保留，新版工作继续研判和授权；用户提交的结构化 Goal 保留在编译后的 RunSpec。过期请求保持过期直到显式刷新。仓库扫描在推进游标前核对分页对象；列表未稳定时显示 `rescan_required` 并保留旧位置。

## 固定候选交付（P3）

可选 delivery 从已完成开发的工作准备私有 Git 候选，重新验证固定测试和原始 Goal 硬条件，进行独立审查，发布审查分支及变更请求，并等待确切 SHA 的 CI。默认等待维护者通过平台合并 PR；service_approved 模式在明确授权后执行原生合并，核验实际结果再完成 Work。公开评论另行批准。代码修复复用原生 Run，保留范围并累计次数。分组决定、动作回执和重启恢复共用 HTTP/SDK/Text UI。配置及离线演示见 [仓库交付](DELIVERY.zh-CN.md)。

CI 次数默认作为进度观察：`ci_fix_limit=null`、`ci_fix_warning=2`。研判任务收到修复和重测进度，由模型决定是否继续或修订方案；部署者明确配置整数预算时仍执行硬上限。已有整数配置继续保留该上限；将其设为 `null` 才采用进度观察模式。Service 以 `all` 范围暂停后，可用 `service.configure_ci_limits()` 或 `delivery ci-limits --limit null --warning 2` 更新这些设置，保留已接受的候选和发布事实。

暂停的修复 Run 作为当前执行恢复。此前候选继续提供交付上下文，但不会替代正在开发的 Run；已完成并接受的交接仍由 Delivery 继续推进。

范围内的 CI 修复可请求超出配置上限的一次额外尝试。使用 `service.decide(request, "approve", allow_extra_attempt=True)` 或 CLI／Text UI 的 `approve ... --allow-extra-attempt` 明确授权；扩展只属于当前方案的下一次尝试。`service.refresh_delivery()` 可续期已过期且尚未决定的 CI 请求，或继续已接受的准备过程，包括基线冲突修复。此前的 Run、验证与决定继续保留为历史记录。

候选测试失败时保留回执和可读输出，由模型研判原因。代码修复使用有授权的原生 Run；本地临时故障可以请求同一候选重测，只有明确配置 `ci_fix_limit` 时才限制自动重测次数。候选检查通过后才能发布。`work.progress()` 显示交付状态，健康检查提示当前阻塞候选。解决阻塞后，可用 `service.refresh_delivery(changeset_id)` 显式重新核验已收尾、尚未发布的候选，保留失败历史。后续需求修订使用经验证且已同步的集成结果作为本轮基线，历史交付记录继续保留。

若已批准的 CI 修复在创建 Run 前准备失败，同一操作会继续该准备过程，保留原候选验证记录；尚未批准或已经创建修复 Run 的情况仍走对应原生流程。

发布文字与代码审查分别记录。审查已提供的标题和正文直接用于计划；尚未预授权默认模板且缺少正文时，Service 使用独立的发布文字任务，依据已接受需求、审查和验证事实起草。具体发布正文被拒绝后，拒绝反馈交给模型更正，新的文字仍需新的明确审批。代码候选、已通过审查、质量回执和已确认分支继续复用；模型要求部署者方向时等待新的执行说明，调用失败时可重试对应发布任务。健康和工作进度显示当前拒绝及其原因。

候选审查与 CI 研判会获得绑定 Work 的当前执行说明、已接受需求、开发交接及历史验证证据。记录读取使用同一证据投影，内部文件清单仍留在存储中；执行说明发生变化时需要重新提交提议。重试失败研判会保留错误历史和候选回执，后续修复 Run 继承当前说明。健康检查与 Work 进度显示对应研判 job 和实际失败原因。

修复后的候选审查会覆盖相对目标基线的完整拟交付变更，末次修复单独提供。`maintenance.job_call_warning` 默认在调用模型 40 次后提供软提醒，后续提醒间隔逐步增加，模型可以继续评估证据。`job_turns` 默认为 `null`，显式配置整数时才执行调用次数硬限制。`job_seconds` 默认为 null，显式有限值才限制耗时，验证和发布要求继续生效。

## Service 运维（P4）

[Service 运维](SERVICE_OPERATIONS.zh-CN.md) 提供健康事实、原生写入者停靠、跨工作区保留、备份集合/恢复、固定仓库审查及明确的兼容版本接替。CLI、Text UI 和 Python 共用认证控制方法。

可选社区沟通独立于代码交付，支持 Issue 多轮澄清和外部 PR 审查。详见[社区讨论](COMMUNICATION.zh-CN.md)。


## 重试阻塞的开发 Run

先续 Work 的当前授权，再执行 `homebrew-harness service work SERVICE_ID retry WORK_ID --request-id retry-1`；可用 `--seconds` 指定同一原生 Run/checkpoint 的有界重试窗口。Python 对应 `service.work.get(work_id).retry(...)`。

预期可恢复的模型输出失败作为观察交给 Lead 判断；模型可继续调用普通工具，或报告 Run 阻塞原因。控制器保留错误事实并执行已有运行限额。权限、存储完整性和未查证工具结果仍使用原有恢复路径。


[执行视图与控制](EXECUTION.zh-CN.md)

Run 的原始需求、已接受补充与维护计划合计最多可包含 2,000,000 个字符。选择的模型 profile 仍需提供足够的上下文，正常的模型请求上下文检查继续生效。

同一 Work 的只读恢复方案和停机预算迁移见[恢复与兼容说明](RECOVERY.zh-CN.md)。

See [Work sources and execution control](WORK_CONTROL.zh-CN.md) for source selection, same-Work continuation and interactive candidate verification.
