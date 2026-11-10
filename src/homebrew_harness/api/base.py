"""Common observation and waiting; execution control remains mode-specific."""

from functools import wraps
from .types import SDKError, InvalidInput


def host_operation(function):
    """Fence accepted host calls against bounded shutdown."""

    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self.core.operation():
            try:
                return function(self, *args, **kwargs)
            except SDKError:
                raise
            except ValueError as error:
                raise InvalidInput(str(error)) from error
            except OSError as error:
                raise SDKError(
                    "Host persistence or resource operation failed ("
                    + type(error).__name__
                    + "). Inspect accepted state before retrying.",
                    code="operation_unknown",
                ) from error
            except RuntimeError as error:
                from homebrew_harness.storage.durable_runtime import RecoveryRequired
                from homebrew_harness.orchestration.workflow_runtime import (
                    WorkflowError,
                )

                if isinstance(error, RecoveryRequired):
                    raise SDKError(str(error), code="recovery_required") from error
                if isinstance(error, WorkflowError):
                    raise InvalidInput(str(error)) from error
                raise

    return invoke


from homebrew_harness.observation import Handle as Handle
