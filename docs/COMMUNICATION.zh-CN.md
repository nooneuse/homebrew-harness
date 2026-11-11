# 社区讨论与外部 PR 审查

[English](COMMUNICATION.md) | **简体中文** · [Service](SERVICES.zh-CN.md)

可选沟通模块围绕 Issue 和变更请求进行多轮讨论，保存问题、事实、需求草案与审查发现，再独立授权和发布普通评论、线程回复或正式 review。它可以在开发前运行，无需 ChangeSet。

## 配置

在 repository_maintenance 的 ServiceSpec 中增加 communication。项目需已有 repository source，完整配置见[英文文档](COMMUNICATION.md#host-configuration)。projects 使用 provider/instance/project 和宿主环境变量引用；凭据与真实接入地址保存在两个工作区之外。remote 模式要求 owner-only 的独立 credentials_file，写凭据不进入模型或开发环境；local 只接受 synthetic 的本地协议服务。

默认每半天发现和处理一批新材料，auto_intents 为空，公开发言需要批准。可明确预授权配置项目内的 acknowledge/clarify/followup。正式 review 和项目路线承诺仍需操作者决定，提交 review 不自动合并或关闭 PR。

## 多轮讨论

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

`service.requests_page(limit=20)` 分页查询维护决定；`service.models()` 查看连接故障。修复连接环境后，用 `service.recover_model(profile, revision=..., request_id=...)` 确认当前故障，失败 job 另行重试。需要重新读取凭据时先重启服务。

```sh
homebrew-harness service requests SERVICE_ID --limit 20
homebrew-harness service models SERVICE_ID list
homebrew-harness service models SERVICE_ID recover PROFILE --revision FAULT_REVISION --request-id recover-1
homebrew-harness service communication SERVICE_ID inspect ACTION_ID --conclusion retry_not_executed --confirm-not-executed --revision ACTION_REVISION --request-id inspect-2
```
