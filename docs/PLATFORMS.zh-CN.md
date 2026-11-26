# 代码管理平台

[English](PLATFORMS.md)

仓库维护通过平台适配器获取外部数据。工作项、研判、授权、对话、报告和原生执行使用通用契约，当前实现的 provider 为 GitHub。

| 能力 | GitHub | 其他平台 |
| --- | --- | --- |
| 仓库 Issue、变更请求与评论 | 已实现 | 需要适配器 |
| 已知变更请求的评论、审查与检查 | 已实现 | 需要适配器 |
| 审查/目标分支发布、变更请求创建/更新、公开评论 | 已实现，见 [仓库交付](DELIVERY.zh-CN.md) | 需要交付适配器 |

未实现的 provider/能力在宿主配置阶段明确拒绝。修改接入地址不会转换 API；未来 provider 在宿主目录中登记项目验证器和已实现适配器。

## 仓库 Source

外部 ServiceSpec 使用明确的平台声明：

```json
{
  "name": "project",
  "provider": "github",
  "instance": "primary",
  "project": "OWNER/PROJECT",
  "api_url_env": "PROJECT_API_ENDPOINT",
  "token_env": "PROJECT_READ_TOKEN",
  "poll_seconds": 43200,
  "max_pages": 5
}
```

instance 是平台部署的稳定宿主别名，project 由具体适配器验证：GitHub 使用 owner/project，其他适配器可接受项目 ID 或多层 namespace。连接值只放外部宿主文件，同一 provider/instance 必须解析到一致的宿主端点与证书绑定。

仓库源可按需增加可选的 `"credentials_file": "/path/to/private/source.env"`，仅从该外部、仅属主可读、无软链且位于控制目录与工作区之外的文件读取 `api_url_env`、`token_env` 与可选 `ca_file_env`（不回退到宿主／模型环境）。此项并非强制：既可省略 `credentials_file` 从宿主环境读取，也可指向独立只读凭据文件，或复用宿主发布凭据文件而无需把写 token 放进模型／开发环境；独立只读 token 有助于最小权限隔离，但不强制要求。

已有配置省略 `credentials_file` 时不受影响。由于 `credentials_file` 参与持久化平台绑定，将已绑定的 Run 从宿主环境变量迁移到凭据文件（或修改凭据文件路径）会改变绑定身份，需要新建 Run；已有 Service 按 ID 重启时会继续沿用已保存的 ServiceSpec，编辑外部 spec 文件不会生效，切换到文件凭据需新建或替换 Service。

私有 CA 实例可增加 ca_file_env，指向环境变量中的 CA 文件绝对路径；文件位于可变工作区之外。证书显式加载并参与连接绑定，HTTPS 校验保持开启，公共端点默认使用普通证书信任。

## Run 反馈

外部 Run 控制配置使用 change_request：

```json
{
  "version": 1,
  "change_request": {
    "provider": "github",
    "instance": "primary",
    "project": "OWNER/PROJECT",
    "number": 1,
    "actor_ids": [7],
    "app_ids": [11],
    "api_url_env": "PROJECT_API_ENDPOINT",
    "token_env": "PROJECT_READ_TOKEN",
    "poll_seconds": 30
  }
}
```

`change_request` 同样支持可选的外部仅属主 `credentials_file`（位于工作区外、无软链），配置后仅从该文件读取 `api_url_env`、`token_env` 和可选 `ca_file_env`，不继承宿主环境。actor/app ID 按宿主策略选择执行反馈，不授予工具权限。原生控制器继续检查本地/current head、对象版本、inbox 容量和稳定事件身份，再提交反馈；通知采用原有独立声明。

## 身份与状态

ObjectRef 包含 provider、instance、project、kind 和稳定不透明 ID，number 仅用于显示。ID 在完整命名空间内唯一，不要求跨平台全局唯一。GitHub 的 Issue/变更请求使用稳定项目编号，评论、审查和检查使用服务对象 ID。

规范化事件明确 object 与 subject：Issue/变更请求更新指向自身，评论指向父 subject。工作关联采用完整 subject 引用，因此不同项目、实例、平台以及 Issue/变更请求的相同编号不会混淆。重复 feed 可共用工作与事件，采集游标仍独立。

适配器返回有界 SourcePage/ChangeFeedback，处理平台路径、字段、认证与分页；Service/Run 控制器管理持久受理、游标提交、权限和恢复。Source 游标内部位置对 Service 不透明，外层绑定适配器和连接身份，不能跨项目或实例重放。

## 扩展边界

platforms/contracts.py 定义数据与协议，registry.py 保存宿主目录，http.py 提供有界传输，platforms/github/ 实现 GitHub 解析。Service/Run 只消费规范化结果。

新增 provider 实现仓库轮询和/或变更反馈，声明能力并验证项目选择。P3 将使用 publish_branch、ensure_change_request、post_comment、merge_change_request 等固定动作和相同引用，授权、回执和未知结果恢复归交付运行时。

维护 Skill 负责需求判断和工作方法，不选择接入地址、解析 API 或批准写入。当前尚未实现 GitLab 等其他平台适配器。
