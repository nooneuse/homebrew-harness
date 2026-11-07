"""shell for the harness."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import homebrew_harness.configuration
import homebrew_harness.tools.files
from homebrew_harness.context.compaction import ArchiveStore, CompactionError
from homebrew_harness.execution.background import ShellRunner
from homebrew_harness.execution.bash_policy import BashPolicyError
from homebrew_harness.execution.execution_backend import BackendUnavailable
from homebrew_harness.tools.tool_outcome import ToolExecutionError


def run_bash(
    command: str,
    workdir: Path,
    timeout: float = 120,
    *,
    runner: ShellRunner | None = None,
    archive_store: ArchiveStore | None = None,
    artifact_profile: str | None = None,
) -> str:
    """Shared command execution and formatting for synchronous/background Bash."""
    env = homebrew_harness.configuration.public_environment(os.environ)
    owned_runner = runner is None
    if runner is None:
        runner = ShellRunner()
    try:
        if artifact_profile is not None:
            if (
                getattr(getattr(runner, "bash_policy", None), "mode", None)
                != "container"
            ):
                raise BashPolicyError(
                    "Artifact collection requires a registered container runner."
                )
            result = runner.run(
                command, workdir, env, timeout, artifact_profile=artifact_profile
            )
        else:
            result = runner.run(command, workdir, env, timeout)
    except subprocess.TimeoutExpired as error:
        return json.dumps(
            {
                "error": f"Command timed out after {timeout:g} seconds; inspect effects before retrying.",
                "outcome": "unknown",
                **(
                    {"execution": error.bash_execution}
                    if hasattr(error, "bash_execution")
                    else {}
                ),
            }
        )
    except ToolExecutionError as error:
        return error.serialize()
    except (BackendUnavailable, BashPolicyError) as error:
        return json.dumps(
            {
                "error": str(error),
                "executed": False,
                **(
                    {"execution": error.bash_execution}
                    if hasattr(error, "bash_execution")
                    else {}
                ),
            }
        )
    except OSError:
        return json.dumps(
            {
                "error": "Cannot start Bash; check its installation and working directory."
            }
        )
    finally:
        if owned_runner:
            runner.close()

    def bounded(text: str) -> str:
        if len(text) <= homebrew_harness.tools.files.OUTPUT_CHAR_LIMIT:
            return text
        return (
            text[: homebrew_harness.tools.files.OUTPUT_CHAR_LIMIT]
            + "\n[output truncated]"
        )

    output = {
        "exit_code": result.returncode,
        "stdout": bounded(result.stdout),
        "stderr": bounded(result.stderr),
    }
    if hasattr(result, "bash_execution"):
        output["execution"] = result.bash_execution
        if result.bash_execution.get("artifacts", {}).get("state") in {
            "export_failed",
            "collection_unknown",
        }:
            output.update(
                error="Command finished but artifact collection failed; inspect the result before retrying.",
                artifact_collection_failed=True,
                outcome="unknown"
                if result.bash_execution.get("artifacts", {}).get("state")
                == "collection_unknown"
                else "partial",
            )
        isolation = result.bash_execution["isolation"]
        if not isolation.get("configuration_verified") or not isolation.get(
            "cleanup_complete"
        ):
            output.update(
                error="Container Bash execution or cleanup could not be verified; inspect the owned run.",
                outcome="unknown",
            )
    if getattr(result, "output_limit", False):
        output.update(
            error="Command exceeded the 8 MiB capture limit and was stopped; output archive is partial.",
            outcome="unknown",
            output_limit=True,
        )
    for stream_name in ("stdout", "stderr"):
        full_output = getattr(result, stream_name)
        if len(full_output) > homebrew_harness.tools.files.OUTPUT_CHAR_LIMIT:
            try:
                output[stream_name + "_path"] = (
                    archive_store or ArchiveStore(workdir)
                ).save("tool-results", full_output)
            except CompactionError:
                output["archive_error"] = (
                    "Command already finished, but its complete output could not be saved."
                )
    return json.dumps(output, ensure_ascii=False)
