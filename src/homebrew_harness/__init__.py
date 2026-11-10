"""Composable agent runtime with explicit permissions and recoverable execution."""

__version__ = "0.1.0"


def __getattr__(name):
    if name in {"RemoteService", "ControlConnection"}:
        from .services import remote

        return getattr(remote, name)
    if name in {"Harness", "AsyncHarness", "HostConfig", "SessionOptions"}:
        from . import api

        return getattr(api, name)
    if name in {"ServiceSpec", "ScheduleSpec"}:
        from .services import spec

        return getattr(spec, name)
    if name in {"ModelRegistry", "ModelSpec"}:
        from .execution import model_registry

        return getattr(model_registry, name)
    if name in {"ArtifactFile", "ArtifactProfile"}:
        from .execution import artifact_policy

        return getattr(artifact_policy, name)
    if name == "MCPExecutionPolicy":
        from .execution.mcp_policy import MCPExecutionPolicy

        return MCPExecutionPolicy
    if name == "BashPolicy":
        from .execution.bash_policy import BashPolicy

        return BashPolicy
    if name == "HarnessSession":
        from .runtime.session import HarnessSession

        return HarnessSession
    if name == "Capabilities":
        from .runtime.capabilities import Capabilities

        return Capabilities
    if name == "LoopComponents":
        from .runtime.loop_components import LoopComponents

        return LoopComponents
    if name == "agent_loop":
        from .runtime.loop import agent_loop

        return agent_loop
    raise AttributeError(name)


__all__ = [
    "RemoteService",
    "ControlConnection",
    "ServiceSpec",
    "ScheduleSpec",
    "Harness",
    "AsyncHarness",
    "HostConfig",
    "SessionOptions",
    "ModelRegistry",
    "ModelSpec",
    "HarnessSession",
    "Capabilities",
    "LoopComponents",
    "agent_loop",
    "BashPolicy",
    "MCPExecutionPolicy",
    "ArtifactFile",
    "ArtifactProfile",
]
