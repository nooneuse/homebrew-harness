# homebrew-harness

**English** | [简体中文](README.zh-CN.md)

A composable Python agent runtime for interactive development, local delegation, and recoverable task execution. It connects model requests, tools, context management, and persistent state.

The model proposes an action, the runtime executes the corresponding tool, and the result becomes input for the next turn. Tasks, Goals, Workflows, and Agent Teams organize work around this shared loop.

## Features

| Area | Capabilities |
| --- | --- |
| Models | Chat Completions, Responses, DashScope Generation, Anthropic Messages; model selection by role |
| Core tools | Read, write, and edit files; match paths; run Bash; update plans; load skills |
| Context | On-demand skill loading, history compaction, full-result archives, optional cross-session memory |
| Orchestration | Subagents, persistent task dependency graphs, Agent Teams, background commands, Cron, Workflows |
| Completion | Goals with file and test conditions |
| Interaction | Steering during execution, queued requests, interrupts, and separate tool approvals |
| Execution | Host Bash; optional container backends for Bash, TestRunner, and local MCP; artifact collection and publication |
| State | SQLite checkpoints, durable inboxes, recovery inspection, backups, and history maintenance |
| Services | Persistent work queue, repository triage, multi-round issue discussion, external PR review, fixed-candidate delivery, CI continuation, grouped approvals and Text UI |

## Installation

Requires Python 3.10 or newer. Container backends require Docker and a configured image; the core runtime runs on the host.

From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The base installation supports all four model protocols over HTTP. Vendor SDKs are optional. Install the backend you prefer:

```sh
python -m pip install -e ".[openai]"
python -m pip install -e ".[anthropic]"
python -m pip install -e ".[dashscope]"
```

With `transport: "auto"`, a new connection uses an installed, supported SDK when available, otherwise HTTP. Use `"http"` or `"sdk"` to select a backend explicitly. See [model connections](docs/MODELS.md).

Installation provides one command with five subcommands:

```sh
homebrew-harness --help
homebrew-harness session --help
homebrew-harness run --help
homebrew-harness service --help
homebrew-harness workflow --help
homebrew-harness admin --help
```

You can also use `python -m homebrew_harness` with the same subcommands. Running the command without a subcommand starts automatic interaction. New tasks select an interactive Session or a managed Run and the required capabilities; active text steers the current task. Use `session` explicitly for a one-shot task or session-specific options.

`--cwd` and `--env-file` are shared options and may appear before or after subcommands. If repeated, the last explicit value takes precedence. Other options belong to their feature group; use `homebrew-harness run --help` or the help for an individual operation to see them.

## Model configuration

Keep configuration outside the workspace. Create the default environment file:

```sh
mkdir -p ~/.config/homebrew-harness
cp examples/config/.env.example ~/.config/homebrew-harness/.env
chmod 600 ~/.config/homebrew-harness/.env
```

Set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` in that file. The single-model entry point uses Chat Completions. Exported environment variables take precedence over file values.

Use the [model catalog template](examples/config/.models.example.json) for multiple models or other protocols. `connections` declare protocols and environment variable names, `profiles` declare models and their capabilities, and `defaults` select profiles for the Lead and teammate roles.

```sh
cp examples/config/.models.example.json ~/.config/homebrew-harness/models.json
homebrew-harness session --models-config ~/.config/homebrew-harness/models.json
```

Set the environment variables referenced by the template, and configure tool use, structured output, and reasoning capabilities for each model. The Lead can select an alias with `--lead-model`; teammates and subagents can select from the host's registered profiles. Credentials and model endpoint values belong in the host environment.

## Quick start

```sh
# Automatic interaction
homebrew-harness

# One-shot task
homebrew-harness session "Read the project files and explain how the core modules relate"

# Explicit working directory
mkdir -p /tmp/harness-example
homebrew-harness session --cwd /tmp/harness-example \
  "Create hello.py that prints Hello, world!, then read it back to verify"

# Resume the latest saved session
homebrew-harness session --resume-session
```

The working directory defaults to the directory from which you launch the command. `--cwd` selects another workspace; session state is stored there.

File queries inspect source files with bounded results and skip Git, dependency, and runtime directories by default. Reserved runtime paths and their case variants remain host-managed. Explicit full-result references use their owning context.

The explicit Session entry defaults to the `core` profile with basic tools. Enable optional modules as needed:

```sh
homebrew-harness session --enable subagents --enable tasks
homebrew-harness session --enable teams --enable worktrees
homebrew-harness session --enable background --enable cron
homebrew-harness session --memory-mode recall
```

`--profile full` enables all optional modules and defaults memory to `recall`. `--memory-mode auto` enables memory maintenance after normal user turns; `off` disables memory calls.

Ask "Help me host this project" to use the installed `host-project` skill. It collects deployment choices, prepares private configuration and guides supervision of a repository-maintenance Service. See [repository hosting](docs/HOSTING.md).

The example skills cover [Python code review](skills/code-review/SKILL.md) and [text summarization](skills/text-summary/SKILL.md).

## Interactive slash commands

The default interactive entry and `homebrew-harness session` support these commands. Host commands are handled directly; unknown commands report an error. Python API text remains task material rather than CLI control syntax.

| Command | Behavior |
| --- | --- |
| `/help` | Show available command syntax. |
| `/model` | Show the current model, protocol and registered aliases. |
| `/model NAME` | Select an alias from the loaded model catalog; without a catalog, select a model name on the existing connection. |
| `/reasoning` | Show the current reasoning/thinking options. |
| `/reasoning LEVEL` | Set `none`, `minimal`, `low`, `medium`, `high` or `xhigh` for Chat/Responses; `on`/`off` selects thinking where mapped. `default` restores configured options. |
| `/compact` | Compact completed history, preserving the original requirements, current plan and full archives. |
| `/new` | Start blank context after accepted work ends, retaining model choice. Old history remains saved. |
| `/diff [--base REF] [PATH ...]` | Inspect a fixed Git base against the working tree, including untracked text. Quote paths containing spaces. |
| `/review [--base REF] [PATH ...]` | Review a bounded snapshot using only reading/search/Git tools. Findings refer to the recorded snapshot; later changes mark the result stale. |
| `/skills` | List available skills. |
| `/skills show NAME` | Read a skill's instructions. |
| `/skills use NAME TASK` | Load the chosen skill as guidance for an explicit task. |
| `/worktree [list]` / `/worktree show NAME` | Inspect Git worktrees and task ownership. |
| `/worktree create NAME --task TASK_ID` | Bind a clean HEAD worktree to a pending task; requires tasks/worktrees. |
| `/worktree remove NAME` | Remove an unused, unchanged managed worktree after its task completes; retain its branch. |
| `/memory` / `/memory list` / `/memory show NAME` | Inspect stored cross-session knowledge and the configured memory mode. |
| `/memory correct NAME TEXT` / `/memory delete NAME` | Atomically correct or remove a record, including when automatic memory is off. |
| `/resume` / `/resume list` / `/resume ID` | Continue paused work, list retained contexts, or restore a selected context. |
| `/pwd` | Show the base workspace and actual Lead directory, branch and task/worktree ownership. |
| `/ps` / `/ps show ID` | Inspect owned operations, resources and pending cleanup. |
| `/stop` / `/stop ID` | Pause current execution or request cancellation of an owned operation. |
| `/hooks` / `/hooks show EVENT` | Show callback order and recent failures. |
| `/agents` / `/agents show NAME_OR_ID` | Inspect the Lead, teammates and subagents, including their model and task. |
| `/quit` / `/exit` | Save state and close the interface, using the same path as `q`, `quit` and `exit`. |

Model and reasoning changes are queued when the Lead is busy and apply after its current turn. They retain readable history and user requirements, archive private protocol replay, and persist in the Session checkpoint. The default entry also retains its interactive choice for later tasks and reopening. Configuration sources must still match for recovery. Existing members and independently bound Workflows retain their own model. A managed Run keeps its declared model contract; use its native model handoff for changes.

Query commands do not call a model or the task router. `/compact` and `/review` use the existing model request deadlines. Truncated review snapshots require narrower paths. `/new` requires accepted work and goals to be finished or cancelled; it keeps history available for explicit recovery and does not carry previous approvals into the new conversation. These commands do not configure credentials or change global model files.

`/stop` is accepted without waiting for a model reply. Cancellation and actual resource release are separate states shown by `/ps`; inspect `/recovery` when an operation's effects are uncertain. Ordinary inputs remain queued while a Session is paused, including after reopening; `/resume` continues after cleanup and inspection. Specific operation IDs come from `/ps`, rather than system process IDs. A managed Run uses its native pause control; foreign worker observations are labelled as checkpoint snapshots.

`/resume list` shows workspace-local contexts. `session:ID` restores readable history and waits for the next request; interaction and Run IDs continue their original task. The saved configuration and model sources must match. Finish the default entry's current context before choosing another; an explicit Session can switch after its execution ends and cleanup completes. Retired history remains under the existing admin recovery policy.

Memory queries and manual edits do not enable automatic extraction or invoke a model. Hooks are read-only; agent creation and steering continue through existing tools and `/steer-to`. Worktree creation follows task binding: claiming the task selects its directory. `/quit` and `/exit` pause an attached Run; a previously requested detached Run follows its registered background lifetime.

## Automatic interaction

Start `homebrew-harness` in the project directory and enter a task. The host plans new tasks, checks its capability catalog, and prepares the selected Session or managed Run. Each task keeps its original requirements, chosen capabilities, model and execution policy. Ordinary input during execution steers that task; `/queue TEXT` creates an independent follow-up. Completed discussions become reference for the next task, with complete archived content available on demand. Native reasoning and replay stay in their source context.

Optional host settings live outside the workspace:

```sh
cp examples/config/interaction.example.json ~/.config/homebrew-harness/interaction.json
homebrew-harness --host-config ~/.config/homebrew-harness/interaction.json
```

`HARNESS_INTERACTION_CONFIG` can select that file. It accepts external `models_config`, `env_file`, `test_config`, `bash_config`, `mcp_config` and `control_config` references. `allowed` declares available optional modules; the task selects a subset. Set `demo_workflows` to register the bundled review script, or include `mcp` with a configured server catalog. `memory_mode` is independent. `managed_bash` exposes Bash to automatic Runs while retaining per-call approval; its default is false. Registered tests keep their existing authorization policy.

A new task uses one bounded planning request and carries the result into execution. Steering and host controls do not invoke the router. `prefer_session: true` skips routing and fixes new tasks to core Session mode; `model_planning: false` uses core interactive tasks unless `/managed TEXT` explicitly requests a Run.

| Input | Automatic host behavior |
| --- | --- |
| `/status` | Inspect the active task, requests, and actual cleanup state |
| `/managed TEXT` | Queue an explicitly managed task |
| `/pause`, `/resume`, `/cancel` | Control the current task; pause applies to Runs |
| `/upgrade` or `continue as managed` | Continue a settled single-agent Session as a Run |
| `/detach` or `keep running after closing` | Request that a managed task continue after the interface closes |
| `/approve ID REVISION yes/no` | Decide an exact persistent tool request |
| `/answer ID REVISION TEXT` | Answer a requirement question |
| `/grant ID REVISION RUNS SECONDS` | Renew a fixed test grant with explicit bounds |

A unique displayed tool request also accepts a complete short answer such as `yes` or `no`; mixed prose becomes steering. A unique displayed requirement question accepts a plain answer. Test grants continue to require their exact bounds. Piped input does not acquire interactive approval authority.

The first release has one active task per workspace. Task/Team/worktree, Cron, unfinished asynchronous work, and uncertain operations prevent Session-to-Run handoff. A selected MCP Session can hand off after its actors close; the target verifies the same server declaration and tool manifest. A handoff preserves original requirements, known results, archive references, observed usage, the remaining deadline and remaining test grants. The old Session retains its records and stops accepting resumed execution; the target obtains fresh verification evidence.

Closing an attached interface stops its owned execution with bounded cleanup. `/detach` explicitly requests an independent supervisor; `detached: true` is a host lifecycle preference for automatically planned managed tasks. Reopening attaches to the existing Run. Startup uses a durable ACK and execution leases. If startup is uncertain, `/status` exposes its token; after inspecting stopped execution, `/supervisor-recover TOKEN` fences the old launch, and `/resume` can retry. Run supports selected MCP tools; Cron remains available in Sessions.

## Managed runs (hands-off)

`homebrew-harness run` persists a task as a Run and advances it through bounded Episodes in a separate worker process. Episodes continue the same session; process recovery checks the checkpoint and actual execution state. The Run's overall deadline includes waiting and restarts. Runs can use declared stages and a fixed team of members.

```sh
cp examples/config/run.example.json ~/.config/homebrew-harness/run.json
homebrew-harness run --cwd /path/to/project start \
  --spec ~/.config/homebrew-harness/run.json
```

### Control and recovery

Startup prints a `run_id`. Manage it from another terminal:

```sh
homebrew-harness run --cwd /path/to/project status RUN_ID
homebrew-harness run --cwd /path/to/project steer RUN_ID "Add a requirement to the current task"
homebrew-harness run --cwd /path/to/project pause RUN_ID
homebrew-harness run --cwd /path/to/project resume RUN_ID
homebrew-harness run --cwd /path/to/project cancel RUN_ID
```

`supervise RUN_ID` takes over a recoverable task after the supervisor restarts. `steer --mode queued` applies input at the next Episode; `--mode interrupt` requests cancellation of the current cancellable step. Use a new Run for an independent task.

The default tools cover files, context, and tests. Stage endpoints are included only when the Run has the corresponding stage capabilities. `allow_tests` preauthorizes fixed test recipes. `enabled` can add subagents, background execution, or registered Workflows; `tools` selects their tools. Use `--env-file`, `--models-config`, `--test-config`, `--bash-config`, and `--mcp-config` to select external host configuration.

Run status includes outcomes, stop reasons, heartbeats, member outcomes, and cleanup state. `recovery RUN_ID` lists operations requiring inspection. After checking their effects, use `inspect RUN_ID OPERATION_ID REVISION` and explicitly resume. Recovery waits for the previous execution to finish cleanup.

Model request timeouts use bounded recovery. An expired Episode saves its state and continues; Run or Session deadlines stop execution. Late responses are discarded, and unfinished execution remains tracked for cleanup.

After cancellation or final failure, unfinished Tasks and Goals become `abandoned` once execution and unknown effects are settled. Accepted results remain available. `finalized_at` marks completed ownership cleanup, allowing a new Run in the workspace.

### Approvals and test grants

When a tool needs permission, the Run records an exact request and enters `waiting`. Requirement questions use separate Attention requests. Inspect and answer the current revision:

```sh
homebrew-harness run --cwd /path/to/project requests RUN_ID
homebrew-harness run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes
homebrew-harness run --cwd /path/to/project answer RUN_ID QUESTION_ID REVISION "Requirement decision"
```

After approval, the model uses `execute_approved` to submit the original call. Permission is consumed once when the operation is accepted. An Attention answer becomes a user amendment to the original task.

When a member's fixed-test grant expires or runs out, the runtime records a `test_grant` request. Renew it for the same task with a bounded run count and duration:

```sh
homebrew-harness run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes \
  --runs 1 --seconds 600
```

The recipe and Run limits bound the renewal. The member checks the updated grant before continuing. `write_paths` defines writable file prefixes; the host registers fixed tests and Bash backends.

Repeated failures, ineffective writes, and recurring file states trigger local replanning. Persistent lack of progress blocks the Run. Inspect it with `progress RUN_ID`; after execution ends, `progress-reset RUN_ID REVISION --reason TEXT` starts a new replanning cycle.

### Stages and inputs

RunSpec can declare `stages`, each with a `key`, `subject`, `depends_on`, and `acceptance`. Stage acceptance uses structured conditions with `judge: false`; `goal` describes final delivery. See the [staged Run example](examples/config/run-stages.example.json).

Stages enable Task tools. The model claims a task and submits its outputs with `complete_task`. Host acceptance completes the Task, saves a handoff, and unlocks dependencies. Changes to relevant files or requirements invalidate earlier acceptance.

`depends_on` expresses ordering and acceptance dependencies. `inputs` declares predecessor files needed by a consumer, for example `"inputs": [{"stage": "api", "paths": ["api.py"]}]`. The host delivers the accepted version before the consumer claims its task. These inputs are read-only and their versions are linked to stage evidence.

Worktree stages capture content within the declared output, predecessor input, hard-condition, and test ranges. `source_paths` adds repository source dependencies as relative path prefixes and leaves write permissions unchanged. `snapshot_limits` configures `max_files`, `max_bytes`, and `max_paths`: Run settings apply to stage snapshots, while recipe settings apply to test inputs. Outside-scope metadata and Git checks track other changes. Worktrees start from a complete, clean HEAD.

Use `stages RUN_ID` for task status and `handoffs RUN_ID` for original requirements, acceptance facts, and model summaries. The model can read handoffs with `get_handoff`. Test receipts are stored in SQLite and checked against current code and recipes across process recovery.

### Teams, integration, and model handoff

`members` declares fixed names, roles, prompts, model aliases, and recovery limits. A stage selects its member with `executor`; `isolated: true` assigns a separate worktree, and `write_paths` defines output ownership. Member recovery preserves identity, task, and context.

Members wait for pending input amendments to be applied before continuing. An unfinished work segment produces a concrete Lead action item; `member_outcomes` in Run status explains the result. Unknown effects require host inspection.

An integration stage lists predecessors in `integrates`. After producers stop, `integrate_stage` applies accepted outputs to the integration worktree. Once accepted, a stage with `publish: true` can use `publish_stage` to publish manifest outputs to the base workspace. `transfers` lists results; after interruption, `transfers-resolve` checks actual files before related operations are resolved.

`fallback_models` declares backup aliases, and `fallback_errors` selects failure classes for automatic handoff. The host can also use `handoff RUN_ID MODEL_PROFILE REVISION --reason TEXT`. Handoff creates a new Lead session with the original requirements, Tasks, and evidence; member bindings and the overall Run deadline remain in effect. Same-model `resume` records acknowledgment of resolved connection faults with its checkpoint and retains usage observations and deadlines.

### PR sources and notifications

The optional [Run control configuration](examples/config/run-control.example.json) subscribes to an existing GitHub PR and enables minimal notifications. Store it outside the workspace, set the repository, PR, allowed actor/app IDs, and environment variables, and remove unused `github` or `notification` entries.

```sh
homebrew-harness run --cwd /path/to/project \
  --control-config /path/to/private/run-control.json start --spec /path/to/private/run.json
```

The source delivers current-head CI/review feedback and comments from allowed actors as reference input. Full events can be retrieved. Source content grants no tool permission. `events RUN_ID` and `effects RUN_ID` list records.

Notifications are off by default. When enabled, they report decision requests, blocked runs, and completion. Uncertain delivery remains `unknown`; after checking it, use `effects-resolve RUN_ID EFFECT_ID REVISION delivered --reason TEXT`. Continue a Run with its original control configuration.

### Export, backup, and demonstration

`export RUN_ID --output FILE` exports requirements and state. `backup RUN_ID --output DIR` backs up the database and attachments after supervision stops and cleanup completes. `verify-backup DIR` checks the manifest; `restore-backup DIR --output NEW_DIR` restores into a new inspection directory. The host checks workspace and Git identity before execution. `min_free_bytes` sets the runtime disk floor and defaults to 128 MiB.

`search_text`, `git_status`, and `git_diff` provide bounded content and change inspection. Explicit `git_diff` paths select their changes independently of the status display limit, including both visible ends of a rename.

The local integration demonstration creates a text-normalization project and exercises requirement answers, exact approvals, supervisor restarts, PR feedback, stage acceptance, model handoff, member recovery, test-grant renewal, request-timeout recovery, and dependent-file integration in a repository with 5,001 unrelated files. It also checks publication, export, backup, cancellation, a new Run, and same-model resume:

```sh
python examples/managed_run_demo.py --output /tmp/harness-ae-demo
```

Choose an empty output directory. The demonstration uses local HTTP services and deterministic model responses, writes `result.json`, and makes no paid model calls.

## Input during execution

Interactive mode continues reading terminal input while the model works:

| Input | Behavior |
| --- | --- |
| Plain text or `/steer TEXT` | Amend the current Lead task at a safe boundary |
| `/queue TEXT` | Start the next request after current work ends |
| `/interrupt TEXT` | Request cancellation of cancellable work, then apply new requirements |
| `/steer-to NAME TEXT` | Send a user amendment to a named teammate |
| `/inputs` | List pending inputs and results |
| `/inputs cancel ID` | Cancel pending input |
| `/inputs retry ID` | Retry input confirmed as retryable |
| `/approve ID yes` / `/approve ID no` | Answer a specific tool approval |

The original task and amendment order are saved. Completed tool results remain execution facts; remaining proposals are checked again when new input arrives.

Host Bash requests approval per call by default. Explicit Session mode uses exact approval IDs; automatic interaction also adapts a unique displayed focus. A one-shot terminal prompt accepts `y`/`yes`; pressing Enter declines.

## Goals and tests

`/goal CONDITION` sets a completion goal, `/goal` shows its state, `/goal resume` continues it, and `/goal clear` removes it.

Structured Goals support `file_exists`, `file_contains`, `file_sha256`, and `tests_passed`. Save a specification outside the workspace, for example:

```json
{
  "condition": "Create a report and pass the project tests",
  "predicates": [
    {"id": "report", "type": "file_exists", "path": "REPORT.md", "min_bytes": 1},
    {"id": "tests", "type": "tests_passed", "recipe": "unit-tests"}
  ],
  "judge": false
}
```

```sh
homebrew-harness session --allow-tests unit-tests \
  --goal-spec ~/.config/homebrew-harness/goal.json
```

When a task description and `--goal-spec` are supplied together, the description remains the task and the file supplies its completion conditions. The runtime saves both before model work begins. Goal files use bounded UTF-8 JSON input.

A workspace containing `tests/` registers `unit-tests` by default. Custom tests use the [TestRunner template](examples/config/.test-runner.example.json) and `--test-config`. `/tests` lists recipes; `/tests approve NAME` or `--allow-tests NAME` authorizes execution. Results are linked to the tested code snapshot for Goal verification.

## Workflows and MCP

Workflows use host-registered Python async scripts to organize fixed steps, save their results, and reuse them on recovery. The built-in demonstration runs offline:

```sh
homebrew-harness workflow list
homebrew-harness workflow --cwd /tmp/harness-example demo
```

Direct model workflows select a registered profile with `--models-config PATH --model ALIAS`; the single-model environment remains available. The chosen profile supplies its protocol, options, and request limits. Session workflows use the same complete model binding; changing that binding requires a new workflow run.

```sh
homebrew-harness workflow --models-config ~/.config/homebrew-harness/models.json \
  --model planner run review-changes --changes-file changes.txt
homebrew-harness admin benchmark --live \
  --models-config ~/.config/homebrew-harness/models.json --model planner
```

The benchmark is offline by default; `--live` explicitly selects model execution. It checks a small suite of file tasks. Workflow status distinguishes bounded foreground return, confirmed persistence, and actual cleanup. A paused workflow can be resumed after cleanup and its completion record are confirmed; unfinished execution remains owned even when storage is busy.

The main session loads it with `--demo-workflows`. Register custom scripts through `WorkflowRegistry`; see the [design document](docs/DESIGN.md) for component composition.

MCP supports real stdio and Streamable HTTP transports. Start the local demonstration service with:

```sh
python examples/create_mcp_demo_config.py \
  --output ~/.config/homebrew-harness/mcp.json
homebrew-harness session --enable mcp \
  "Connect to the docs service, search for Hooks, and query its version"
```

`/mcp` shows Session connections; `/mcp close CONNECTION_ID` closes one instance. Configuration and demonstration scripts are in [examples](examples/).

Runs select explicit raw tool names from that host configuration. See the [MCP Run example](examples/config/run-mcp.example.json):

```sh
homebrew-harness run --mcp-config ~/.config/homebrew-harness/mcp.json \
  start --spec examples/config/run-mcp.example.json
```

The `mcp` map selects server aliases and tool lists, and enables MCP for the Run. Selected tools enter the same effective pool as file and test tools. Host policies determine permission; `confirm` calls use the usual Run requests and one-time approvals. Recreated workers reconnect and compare the saved declaration and tool schemas. Business state persists in the external service's own storage.

`mcp_delegation` defaults to `lead_only`. Set it to `read_only` to expose selected host `allow` + `read_only` tools to teammates and Subagents within their inherited tool scope. Workspace services prepare a separate input scope for the assigned task. Automatic interaction uses the same selection contract, including detached execution and settled Session handoff.

A lost read response is saved as a failed observation. A write with an uncertain result requires recovery inspection before continuing. Run status includes local stdio resource records for host and container execution. If an owner cannot publish closure, inspect the stopped service and use `run mcp-resource-inspect RUN_ID RESOURCE_ID REVISION --token TOKEN --confirmed-stopped` to record that host decision.

## Optional container execution

Bash selects the container backend with `--bash-mode container --bash-config PATH`. TestRunner and local stdio MCP select containers in their host configuration. Examples:

- [Host Bash](examples/config/bash-host.example.json)
- [Container Bash](examples/config/bash-container.example.json)
- [Artifact collection rules](examples/config/bash-artifacts.example.json)
- [Local MCP container](examples/config/.mcp-container.example.json)

Set the Docker CLI, image digest, and program paths inside the image. Containers receive a workspace copy. To retain generated files, collect artifacts using registered rules and publish them with `publish_artifacts`.

## State and maintenance

Session state lives in `.harness/` under the working directory. Tasks, memory, and worktrees use their own local directories. `/recovery` lists operations needing inspection; after checking them, use the suggested recovery commands.

```sh
homebrew-harness admin storage-status --cwd /tmp/harness-example
homebrew-harness admin checkpoint-list --cwd /tmp/harness-example
homebrew-harness admin gc-preview --cwd /tmp/harness-example
```

`homebrew-harness admin` manages backups, history collection, and checkpoint retirement. The default retirement policy keeps the latest 10 sessions and selects older sessions only after they have been closed and fully cleaned up for more than 15 days. See `homebrew-harness admin --help` for parameters.

## Python API

Persistent services add a durable work queue, fixed interval schedules, native approvals, and a Text UI over an authenticated HTTP control API. Independent applications connect with `RemoteService`; remote operators can use an external `--connection` file. See [Services](docs/SERVICES.md) for setup and lifecycle controls. The manual profile delegates bounded work to managed Runs; repository_maintenance adds platform intake (currently GitHub), scoped requirement decisions, persistent discussions and reports.

```python
from pathlib import Path
from homebrew_harness import Harness, HostConfig

with Harness(workdir=Path.cwd(), config=HostConfig.from_files()) as harness:
    session = harness.sessions.open()
    turn = session.submit("Explain the project's main modules", request_id="overview-1")
    report = turn.wait(120)
    if report.snapshot.output_available:
        print(turn.result().text)
```

`Harness` provides Session, managed Run, Workflow, automatic interaction, approval, and maintenance services. `AsyncHarness` exposes awaitable views. See [Python API](docs/PYTHON_API.md) for configuration, steering, detached execution, recovery, and resource ownership.

The [application example](examples/python_api.py) supports an offline demonstration:

```sh
python examples/python_api.py --demo --workspace /tmp/harness-sdk-example
```

`HarnessSession`, `Capabilities`, `ModelRegistry`, `LoopComponents`, and `agent_loop` remain available for component composition. Module responsibilities and execution flow are described in [docs/DESIGN.md](docs/DESIGN.md).

## Development and license

```sh
python -m unittest discover -s tests -t .
```

Tests cover local protocol services, files and databases, recovery flows, and container integration. Linux Docker cases run when their environment is enabled.

The project uses the [MIT license](LICENSE). Some ideas and code draw on the learn-claude-code tutorial; its upstream license is preserved in [licenses](licenses/learn-claude-code-LICENSE). Runtime dependencies retain their own licenses.

Code-platform integration uses explicit adapters. GitHub is currently implemented; common execution and maintenance state can be reused by future providers. See [Code platforms](docs/PLATFORMS.md). Optional [Repository delivery](docs/DELIVERY.md) adds fixed candidates, local quality checks, CI continuation and PR-based delivery. Maintainers merge by default; an optional mode executes an explicitly approved native platform merge and verifies its actual result.

[Service operations](docs/SERVICE_OPERATIONS.md) cover health, writer parking, retained work evidence, verified backup/restoration, periodic repository audits and explicit compatible runtime upgrades. Run `python examples/service_operations_demo.py --output /tmp/harness-service-operations-demo` for the offline P1-P4 scenario.

Community communication and PR review are optional Service capabilities. See [Community discussion](docs/COMMUNICATION.md) for configuration and control methods.

See [Contributing](CONTRIBUTING.md) for the PR-first contribution workflow.
