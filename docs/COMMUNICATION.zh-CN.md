# 社区讨论与外部 PR 审查

[English](COMMUNICATION.md) | **简体中文** · [Service](SERVICES.zh-CN.md)

可选沟通模块围绕 Issue 和变更请求进行多轮讨论，保存问题、事实、需求草案与审查发现，再独立授权和发布普通评论、线程回复或正式 review。它可以在开发前运行，无需 ChangeSet。

## 配置

在 repository_maintenance 的 ServiceSpec 中增加 communication。项目需已有 repository source，完整配置见[英文文档](COMMUNICATION.md#host-configuration)。projects 使用 provider/instance/project 和宿主环境变量引用；凭据与真实接入地址保存在两个工作区之外。remote 模式要求 owner-only 的独立 credentials_file，写凭据不进入模型或开发环境；local 只接受 synthetic 的本地协议服务。

默认每半天发现和处理一批新材料，auto_intents 为空，公开发言需要批准。可明确预授权配置项目内的 acknowledge/clarify/followup。正式 review 和项目路线承诺仍需操作者决定，提交 review 不自动合并或关闭 PR。

## 多轮讨论

冻结的 job 包含 `no_progress`，表示连续报告无进展的轮次数。这个计数供模型参考，运行时保留模型选择的下一步，不因固定次数强制交给操作者。


新回复推进讨论输入，不直接改变正式 Work 需求、Goal 或开发权限。没有新材料不调用模型，等待作者不占用开发 Run。问题、事实、草案、分歧和每轮结论持久保存；已授权发言进入发送队列，不等下一次半天巡检。

需求足够清晰后产生正式接受请求，接受后创建或更新 proposed Work，再走已有开发授权。替换活动工作的需求前，需要先让原 Run 收尾。方向分歧或多轮无进展交给操作者决定，不按总轮数自动拒绝。

正式接受会冻结完整草案、验收条件、排除项、来源引用和操作者澄清。Run 接收这些已接受条件及只读来源输入。方向问题与需求接受决定按讨论版本保持有效；公开发言许可单独计时，从批准时计算有效期。

发言计划固定对象、正文、intent 与讨论依据。新回答或代码更新可以使未发送计划及旧批准失效。平台确认发表和当前仍适用是不同事实；回执丢失的动作进入 unknown，显式 inspect 查证，不盲目重发。己方已确认发言会关联来源回流，避免自己回复自己。

Service 备份包含讨论记录、发布回执和已准备的 PR 快照。现有保留操作会保留这些沟通历史，不会退休审查快照或讨论记录。

## PR 审查

GitHub 为首个 provider，支持普通评论、reviews、行级评论和 head/base 观察。快照通过平台 API 读取固定 Git 对象，核验 blob 身份并生成只读 head/base 文件。模型只读该版本；基础文件通过 @base/path 读取。

coverage 保存缺失 diff、范围外、二进制、大文件和不支持模式。APPROVE 要求完整覆盖和没有未解决发现；require_tests_for_approval 可以额外要求当前固定测试证据。配置测试时使用宿主 TestRunner，远端第三方代码执行要求容器后端。没有 Docker 时仍可静态审查，明确保存未运行测试的事实。

同一代码版本的新解释可复用快照；新代码产生新依据，旧发现仍保留。作者声称修复或平台点 resolve 不独自证明缺陷解决，行级评论必须定位到固定 diff。

覆盖按每个变更的 head/base 两侧核算，包括删除和重命名。未修改的范围外文件作为可选上下文记录。快照按需保留读取与执行权限，使用前同时验证内容和权限位。准备结果只应用于仍有效的 operation 和讨论版本，晚结果不会重新打开已暂停的议题。

## 使用

Python 提供 service.discussions/discussion/discussion_events、control_discussion、plan_communication、communication/communication_actions、communication_record 和 inspect_communication。prepare_review 先返回持久 operation ID；记录通过分块查询回读。

```sh
homebrew-harness service discussion SERVICE_ID list
homebrew-harness service discussion SERVICE_ID show DISCUSSION_ID
homebrew-harness service discussion SERVICE_ID assess DISCUSSION_ID --request-id assess-1
homebrew-harness service discussion SERVICE_ID prepare-review DISCUSSION_ID --request-id review-1
homebrew-harness service communication SERVICE_ID list
homebrew-harness service communication SERVICE_ID inspect ACTION_ID --request-id inspect-1
```

Text UI 使用同样命令，省去前缀和 Service ID。discussion pause/reopen 控制议题；评论、线程和正规 review 的发布决定复用现有单项/分组审批。Skill 指导讨论与审查，版本、凭据、授权、回执、deadline 和恢复由运行时负责。

## 恢复和请求分页

平台明确拒绝时，动作进入 failed，可以创建修正计划；可能已发表的动作保持 unknown。只有当前版本的明确“已查证未执行”结论才能重新申请授权，查不到回执不会自动重发。公开讨论读取冻结议题与关联证据，内部对话和报告使用自己的任务合同。

`service.requests_page(limit=20)` 分页查询维护决定；`service.models()` 查看连接错误观察；历史错误不会阻止新请求。`service.recover_model(profile, revision=..., request_id=...)` 可清理当前错误观察，失败 job 另行重试。需要重新读取凭据时先重启服务。

```sh
homebrew-harness service requests SERVICE_ID --limit 20
homebrew-harness service models SERVICE_ID list
homebrew-harness service models SERVICE_ID recover PROFILE --revision FAULT_REVISION --request-id recover-1
homebrew-harness service communication SERVICE_ID inspect ACTION_ID --conclusion retry_not_executed --confirm-not-executed --revision ACTION_REVISION --request-id inspect-2
```

## 仓库知识、研判与可配置提示词

较长的 issue 研判可在宿主配置中设置 `request_seconds: 500`，并在 Service 配置中设置 `maintenance.job_seconds: 1500`。请求时限会显式传递给模型传输层。研判与起草共享 1500 秒任务截止时间，每次请求最多使用 500 秒或任务剩余时间。这两个设置与平台 HTTP 请求、自动审批的时限分别配置。

读取代码的 job 会获得有界 project_context，包含源码版本、入口声明、目录导航和文档摘录；可以用 service_read(kind="project_context") 获取并按需阅读相关代码。摘录是取证入口，不是完整功能或验收证明。

assessment_model 默认选择维护 Lead；与 model_profile 的绑定不同时，Lead 先确定事实、问题归属、范围和下一步，讨论模型只起草正文，不改变研判。相同绑定合并处理；外部 PR 仍使用 review_model。questions 的 recipient 为 requester/maintainer，缺省 requester。需求行为、环境、优先级向提出者澄清；架构、文档位置、实现归属和 CI 资源交维护者决定。公开 communication.question_ids 只能引用当前开放的 requester 问题。只剩内部问题时使用 operator，信息充分可以零提问。字段校验不能代替对自由正文的语义判断。

完成的研判先保存在当前 job。重试在输入、源码证据、政策、模板和模型版本有效时复用；纯确认评论可直接消费事件，不重复提问或模型调用，实质变更重新研判。缓存历史缺失会重新研判。阶段共享原 job 时间边界，保留每阶段轮次上限。研判不授予开发或发布权限，审批仍独立执行。

内置 guidance 位于 services/prompts/discussion-assessment.md 和 discussion-draft.md。communication.prompt_files 可以通过 assessment/draft 两个键选择仓库与控制目录外的绝对 UTF-8 文件路径，无需修改 Python。文件不能是 symlink，每个文件最多 16 KiB、8,000 字符。新 job 读取新快照；已有 job 及其重试始终使用原快照。对当前议题应用新文字，使用现有 discussion assess 控制产生新的研判；更换文件路径仍走配置替换流程。模板不能改变工具、结果校验或权限边界。

模型思考强度仍通过模型目录 options 配置，可为研判单独选择高强度 profile，不必将所有回复调到最高强度。
