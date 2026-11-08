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

## Installation

Requires Python 3.10 or newer. Container backends require Docker and a configured image; the core runtime runs on the host.

From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Installation provides four commands:

```sh
homebrew-harness --help
harness-run --help
harness-workflow --help
harness-admin --help
```

You can also launch the main program with `python -m homebrew_harness`.

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
homebrew-harness --models-config ~/.config/homebrew-harness/models.json
```

Set the environment variables referenced by the template, and configure tool use, structured output, and reasoning capabilities for each model. The Lead can select an alias with `--lead-model`; teammates and subagents can select from the host's registered profiles. Credentials and model endpoint values belong in the host environment.

## Quick start

```sh
# Interactive session
homebrew-harness

# One-shot task
homebrew-harness "Read the project files and explain how the core modules relate"

# Explicit working directory
mkdir -p /tmp/harness-example
homebrew-harness --cwd /tmp/harness-example \
  "Create hello.py that prints Hello, world!, then read it back to verify"

# Resume the latest saved session
homebrew-harness --resume-session
```

The working directory defaults to the directory from which you launch the command. `--cwd` selects another workspace; session state is stored there.

The default `core` profile includes the basic tools. Enable optional modules as needed:

```sh
homebrew-harness --enable subagents --enable tasks
homebrew-harness --enable teams --enable worktrees
homebrew-harness --enable background --enable cron
homebrew-harness --memory-mode recall
```

`--profile full` enables all optional modules and defaults memory to `recall`. `--memory-mode auto` enables memory maintenance after normal user turns; `off` disables memory calls.

The bundled skill examples cover [Python code review](skills/code-review/SKILL.md) and [text summarization](skills/text-summary/SKILL.md).

## Managed runs (hands-off)

`harness-run` persists a task as a Run and advances it through bounded Episodes in a separate worker process. Episodes continue the same session; process recovery checks the checkpoint and actual execution state. The Run's overall deadline includes waiting and restarts. Runs can use declared stages and a fixed team of members.

```sh
cp examples/config/run.example.json ~/.config/homebrew-harness/run.json
harness-run --cwd /path/to/project start \
  --spec ~/.config/homebrew-harness/run.json
```

### Control and recovery

Startup prints a `run_id`. Manage it from another terminal:

```sh
harness-run --cwd /path/to/project status RUN_ID
harness-run --cwd /path/to/project steer RUN_ID "Add a requirement to the current task"
harness-run --cwd /path/to/project pause RUN_ID
harness-run --cwd /path/to/project resume RUN_ID
harness-run --cwd /path/to/project cancel RUN_ID
```

`supervise RUN_ID` takes over a recoverable task after the supervisor restarts. `steer --mode queued` applies input at the next Episode; `--mode interrupt` requests cancellation of the current cancellable step. Use a new Run for an independent task.

The default tools cover files, context, and tests. `allow_tests` preauthorizes fixed test recipes. `enabled` can add subagents, background execution, or registered Workflows; `tools` selects their tools. Use `--env-file`, `--models-config`, `--test-config`, and `--bash-config` to select external host configuration.

Run status includes outcomes, stop reasons, heartbeats, member outcomes, and cleanup state. `recovery RUN_ID` lists operations requiring inspection. After checking their effects, use `inspect RUN_ID OPERATION_ID REVISION` and explicitly resume. Recovery waits for the previous execution to finish cleanup.

Model request timeouts use bounded recovery. An expired Episode saves its state and continues; Run or Session deadlines stop execution. Late responses are discarded, and unfinished execution remains tracked for cleanup.

After cancellation or final failure, unfinished Tasks and Goals become `abandoned` once execution and unknown effects are settled. Accepted results remain available. `finalized_at` marks completed ownership cleanup, allowing a new Run in the workspace.

### Approvals and test grants

When a tool needs permission, the Run records an exact request and enters `waiting`. Requirement questions use separate Attention requests. Inspect and answer the current revision:

```sh
harness-run --cwd /path/to/project requests RUN_ID
harness-run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes
harness-run --cwd /path/to/project answer RUN_ID QUESTION_ID REVISION "Requirement decision"
```

After approval, the model uses `execute_approved` to submit the original call. Permission is consumed once when the operation is accepted. An Attention answer becomes a user amendment to the original task.

When a member's fixed-test grant expires or runs out, the runtime records a `test_grant` request. Renew it for the same task with a bounded run count and duration:

```sh
harness-run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes \
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
harness-run --cwd /path/to/project \
  --control-config /path/to/private/run-control.json start --spec /path/to/private/run.json
```

The source delivers current-head CI/review feedback and comments from allowed actors as reference input. Full events can be retrieved. Source content grants no tool permission. `events RUN_ID` and `effects RUN_ID` list records.

Notifications are off by default. When enabled, they report decision requests, blocked runs, and completion. Uncertain delivery remains `unknown`; after checking it, use `effects-resolve RUN_ID EFFECT_ID REVISION delivered --reason TEXT`. Continue a Run with its original control configuration.

### Export, backup, and demonstration

`export RUN_ID --output FILE` exports requirements and state. `backup RUN_ID --output DIR` backs up the database and attachments after supervision stops and cleanup completes. `verify-backup DIR` checks the manifest; `restore-backup DIR --output NEW_DIR` restores into a new inspection directory. The host checks workspace and Git identity before execution. `min_free_bytes` sets the runtime disk floor and defaults to 128 MiB.

`search_text`, `git_status`, and `git_diff` provide bounded content and change inspection.

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

Host Bash requests approval per call by default. Interactive mode uses exact approval IDs. A one-shot terminal prompt accepts `y`/`yes`; pressing Enter declines.

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
homebrew-harness --allow-tests unit-tests \
  --goal-spec ~/.config/homebrew-harness/goal.json
```

A workspace containing `tests/` registers `unit-tests` by default. Custom tests use the [TestRunner template](examples/config/.test-runner.example.json) and `--test-config`. `/tests` lists recipes; `/tests approve NAME` or `--allow-tests NAME` authorizes execution. Results are linked to the tested code snapshot for Goal verification.

## Workflows and MCP

Workflows use host-registered Python async scripts to organize fixed steps, save their results, and reuse them on recovery. The built-in demonstration runs offline:

```sh
harness-workflow list
harness-workflow --cwd /tmp/harness-example demo
```

The main session loads it with `--demo-workflows`. Register custom scripts through `WorkflowRegistry`; see the [design document](docs/DESIGN.md) for component composition.

MCP supports real stdio and Streamable HTTP transports. Start the local demonstration service with:

```sh
python examples/create_mcp_demo_config.py \
  --output ~/.config/homebrew-harness/mcp.json
homebrew-harness --enable mcp \
  "Connect to the docs service, search for Hooks, and query its version"
```

`/mcp` shows connections; `/mcp close CONNECTION_ID` closes one instance. Configuration and demonstration scripts are in [examples](examples/).

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
harness-admin storage-status --cwd /tmp/harness-example
harness-admin checkpoint-list --cwd /tmp/harness-example
harness-admin gc-preview --cwd /tmp/harness-example
```

`harness-admin` manages backups, history collection, and checkpoint retirement. The default retirement policy keeps the latest 10 sessions and selects older sessions only after they have been closed and fully cleaned up for more than 15 days. See `harness-admin --help` for parameters.

## Python API

```python
import os
from pathlib import Path
from openai import OpenAI
from homebrew_harness import HarnessSession

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
    max_retries=0,
)

with client:
    with HarnessSession(client, os.environ["OPENAI_MODEL"], Path.cwd()) as session:
        answer = session.run_user("Read the project and explain its main modules")
        print(answer)
```

`HarnessSession` is the usual entry point. `Capabilities`, `ModelRegistry`, `LoopComponents`, and `agent_loop` support host composition. Module responsibilities and execution flow are described in [docs/DESIGN.md](docs/DESIGN.md).

## Development and license

```sh
python -m unittest discover -s tests -t .
```

Tests cover local protocol services, files and databases, recovery flows, and container integration. Linux Docker cases run when their environment is enabled.

The project uses the [MIT license](LICENSE). Some ideas and code draw on the learn-claude-code tutorial; its upstream license is preserved in [licenses](licenses/learn-claude-code-LICENSE). Runtime dependencies retain their own licenses.
