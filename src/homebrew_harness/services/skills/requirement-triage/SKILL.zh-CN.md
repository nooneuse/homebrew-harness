---
name: requirement-triage
description: 研判仓库 Issue、评论及需求更新，核对重复工作、缺失信息、项目价值与执行范围。
---

[English](SKILL.md)

用 service_read 读取当前工作与关联事件全文，预览只用于导航。区分作者诉求、事实证据和部署者决定；依赖现有行为的结论需用 service_read_code 核对代码。

对照宿主目标、非目标和范围目录，查询已有工作，解释价值、兼容影响、维护成本与缺失事实。选择 accept、needs_info、defer、reject、merge 或 decide；重大方向或范围不明时用 decide。merge 只能引用另一个已有工作，不操作平台上的 Issue 或变更请求状态。

可执行工作加载 maintenance-planning。通过 service_propose 提交 JSON：disposition、category、reason、plan，以及适用的 scope、priority、related_work。类别为 bug/feature/documentation/maintenance/question，scope 只能是宿主别名。保存候选后结束，提议本身不授予权限。

使用 project_context 并查证仓库事实，再判断是否需要提问。按谁掌握答案、谁有决定权选择接收者：可以向参与者询问目标、实际环境、优先级及本人是否愿意贡献 PR，不必等其主动提出。协作意愿是可选信息，不是缺失的需求，也不是接纳需求的条件。依据项目惯例和已有授权安排常规分工与文档位置；实际尚未委托的方向、权限、凭据和资源问题才交维护者，不要求参与者分配内部人力。范围已清楚就推进草案，并单独保留有用的可选贡献邀请。贡献回复不授予开发权限，agent 承诺实现前需已有相应授权。
