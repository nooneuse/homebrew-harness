# Host a repository

**English** | [简体中文](HOSTING.zh-CN.md)

The bundled `host-project` skill guides deployment of a supervised repository-maintenance Service. Start the default interaction in the project checkout:

```sh
homebrew-harness
```

Then ask:

```text
Help me host this project: discuss issues, review contributions and develop changes through PRs.
```

You can ask it to use `host-project` explicitly. The skill is included in the installed package and appears alongside workspace skills; no separate installation is needed. Explicit Python callers supplying their own SkillLoader keep their chosen catalog.

## Configuration conversation

For an existing repository, the assistant researches the code first and automatically prepares a project brief and maintenance/configuration proposal. You review that proposal before effective configuration is created. It asks upfront only for access or repository information needed to inspect the code; other missing choices are collected with the consolidated review. Suggested discovery/reporting is twice a day, with a weekly repository review.

## Repository-derived proposal

README and product documentation, public interfaces and representative implementation, tests/build/CI definitions, contribution guidance, PR templates and roadmap records provide the draft's evidence. It covers project purpose, implemented capabilities, architecture, current gaps, verification strategy, proposed maintenance objectives/exclusions, write scope, review focus and writing conventions. Your explicit preferences take precedence.

The proposal records the inspected branch/commit and checkout state. Important conclusions distinguish observed facts, evidence-based inferences, suggested defaults and unknowns. Documented future features are not treated as implemented. Tests present in the tree are not reported as passing, and CI/branch rules remain unverified without actual observations. Repository scripts and tests are not executed merely for this discovery step.

Review/correct the generated summary and fill only what the repository cannot supply, such as deployment/credential references, model preferences and authorization choices. Review acceptance fixes the proposed project direction and configuration; a materially changed baseline updates the relevant part of the proposal. The assistant then creates effective private configuration and validates it. No Service is started while the proposal is awaiting review, and review does not replace subsequent native tool or public-action approval.

Use a dedicated execution checkout, a separate private control directory and configuration files outside both. Install the runtime outside the checkout it maintains. Supply credential **file paths and variable names**; enter actual tokens, endpoints and commit identity into owner-only external files. Publication credentials remain separate from the model/development environment.

The resulting Service enables repository input, multi-round issue discussion, external PR review and PR-first delivery. By default, development proposals and public actions require decisions, and maintainers merge through the platform. You may preauthorize existing narrow development scopes, review-branch/PR publication and routine clarification replies. Formal reviews and service merge actions retain explicit authorization.

## Customize the deployment

State your preferred models and responsibilities, submission identity, PR format, language, coding conventions and other requirements. The assistant maps each choice to its configuration and explains limitations before deployment. Supplied choices take precedence over suggested defaults; requested new models are registered in the private catalog before use.

| Responsibility | ServiceSpec field |
| --- | --- |
| Planning, operator conversations and default reasoning | maintenance.model_profile |
| Coding | run_defaults.model_profile |
| Issue/community replies | communication.model_profile |
| External PR review | communication.review_model |
| Own candidate review and PR draft | delivery.review_model |
| Periodic repository audit | operations.audit_model |

These values are host model aliases. Default Service reasoning jobs share one model selection; independent triage/report/conversation model settings are not provided. Unspecified role overrides use their existing fallback model, which the assistant includes in the summary.

Git author/committer use delivery author_name_env/author_email_env. Push authentication is separate, and the GitHub PR author is determined by the delivery API credentials. Communication credentials determine the author of public replies/reviews. Different identities can use separate private credential files; changing a Git name does not change the PR author.

Persist model-facing preferences in `maintenance.instructions`, for example:

```json
{"instructions": {
  "development": "Follow project typing conventions and avoid unrelated refactors.",
  "discussion": "Use concise community-language replies and concrete questions.",
  "review": "Propose PR title/body with Summary, Changes and Validation sections.",
  "report": "Summarize changes, pending decisions and next steps."
}}
```

Common guidance uses `common`; each job receives only common plus its matching entry. Development uses `development`, while own PR descriptions use `review`. External PR review and issue replies share `discussion`; conditional wording can distinguish their presentation. See [Services](SERVICES.md) for all supported job names.

Writing formats are model guidance, not an enforced template engine. Custom PR drafts use the existing publication proposal and exact-text approval, even when fixed branch/PR actions are preauthorized. Automatic issue-closing keywords are currently rejected. The assistant marks each custom requirement as supported, model-guided, pending or unsupported and explains any alternative; a mandatory unsupported condition is not silently dropped. Enforceable paths, tests and approvals use the existing structured configuration fields.

Delivery uses registered tests. External PR review can be static; executing third-party PR tests requires the container TestRunner backend. Native service execution currently targets supported Unix hosts. Windows clients can supervise the same HTTP control API.

## Validate and deploy

The assistant prepares private configuration and runs:

```sh
homebrew-harness service --cwd CONTROL_DIR validate --spec SERVICE_JSON --host-config HOST_JSON
homebrew-harness service --cwd CONTROL_DIR start --spec SERVICE_JSON --host-config HOST_JSON --detach --request-id DEPLOYMENT_ID
```

Replace markers with the deployment's actual quoted paths and identifiers. Validation checks host/service schemas, file locations, credentials separation, model aliases and named tests. It does not call a model or platform, execute tests, or create runtime state. Platform permissions, Git remote access, branch rules and Docker/image availability still need operational verification. If prerequisites or host execution approval are unavailable, the assistant reports the prepared configuration and remaining steps.

After startup, keep the Service ID. The assistant checks the live owner and repository source results, then provides commands for that deployment. OS startup/restart after reboot is configured separately when requested.

## Supervise the project

Connect with `homebrew-harness service --cwd CONTROL_DIR attach SERVICE_ID`. In the Text UI:

```text
service> status
service> We should prioritize compatibility fixes this month.
service> requests
service> approve REQUEST_ID REVISION --decision-id DECISION_ID
service> work list
service> reports
service> pause execution
service> resume
service> exit
```

Ordinary text discusses direction; `work submit` requests development and `work steer` amends active work. Requests keep their exact target/version, and approval commands decide. `exit` leaves a detached service running; `stop` closes it. A stopped service restarts with `service --cwd CONTROL_DIR start SERVICE_ID --detach`.

The Service follows issues and PR versions, discusses unclear needs over multiple rounds, develops accepted scoped work, verifies candidates, opens PRs and follows CI/review feedback. It observes maintainer merges and verifies integration before completing delivery. One service uses one serial workspace.

See [Services](SERVICES.md), [Communication](COMMUNICATION.md), [Delivery](DELIVERY.md) and [Operations](SERVICE_OPERATIONS.md). Workspace skills belong in `skills/NAME/SKILL.md`; keep names distinct from built-ins. Conflicting names are excluded rather than silently replacing a built-in.
