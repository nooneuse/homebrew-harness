"""Small, serializable host contracts, independent of internal execution objects."""

from dataclasses import asdict, dataclass, field

from homebrew_harness.security.data_boundary import redact


class SDKError(RuntimeError):
    def __init__(self, message, *, code="sdk_error", ref=None, details=None):
        super().__init__(redact(message))
        self.code, self.ref, self.details = code, ref, redact(details or {})


class UnsupportedOperation(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="unsupported_operation", **kw)


class InvalidInput(SDKError, ValueError):
    def __init__(self, message, **kw):
        super().__init__(message, code="invalid_input", **kw)


class StaleReference(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="stale_reference", **kw)


class ResultNotReady(SDKError):
    def __init__(self, message, **kw):
        super().__init__(message, code="result_not_ready", **kw)


class ClosePending(SDKError):
    def __init__(self, report):
        super().__init__(
            "Owned work remains pending after the close window.",
            code="cleanup_pending",
            details=report.data(),
        )
        self.report = report


class Data:
    def data(self):
        return redact(asdict(self))


@dataclass(frozen=True)
class ExecutionRef(Data):
    kind: str
    id: str
    owner_id: str | None = None


@dataclass(frozen=True)
class SubmissionReceipt(Data):
    ref: ExecutionRef
    request_id: str
    accepted: bool
    launch_state: str = "accepted"


@dataclass(frozen=True)
class RequestRef(Data):
    id: str
    revision: int
    target: ExecutionRef
    durable: bool = True

    def __post_init__(self):
        if (
            not isinstance(self.id, str)
            or not self.id
            or type(self.revision) is not int
            or self.revision < 0
            or not isinstance(self.target, ExecutionRef)
            or type(self.durable) is not bool
        ):
            raise InvalidInput(
                "Request references require an ID, nonnegative revision and execution target."
            )


@dataclass(frozen=True)
class HumanRequest(Data):
    ref: RequestRef
    kind: str
    state: str
    tool: str | None = None
    arguments: dict | None = None
    question: str = ""
    reason: str = ""
    expires_at: float | None = None
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionSnapshot(Data):
    ref: ExecutionRef
    state: str
    revision: int = 0
    reason: str = ""
    input_revision: int = 0
    requests: tuple = ()
    output_available: bool = False
    persistence_confirmed: bool = False
    execution_pending: bool = False
    cleanup: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def terminal(self):
        return self.state in {"completed", "failed", "cancelled", "abandoned"}

    @property
    def settled(self):
        return (
            (self.terminal or self.state == "paused")
            and self.persistence_confirmed
            and not self.execution_pending
        )

    @property
    def actionable(self):
        return (
            self.settled
            or self.state
            in {
                "paused",
                "blocked",
                "waiting",
                "waiting_input",
                "needs_attention",
                "startup_pending",
                "unattached",
            }
            or bool(self.requests)
        )


@dataclass(frozen=True)
class ExecutionResult(Data):
    ref: ExecutionRef
    state: str
    text: str | None = None
    value: object = None
    error: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WaitReport(Data):
    snapshot: ExecutionSnapshot
    condition_met: bool
    timed_out: bool = False


@dataclass(frozen=True)
class ExecutionEvent(Data):
    id: str
    kind: str
    snapshot: ExecutionSnapshot


@dataclass(frozen=True)
class CloseReport(Data):
    state: str
    persistence_ok: bool
    resources_closed: bool
    pending: tuple = ()
    errors: tuple = ()
    pending_calls: int = 0
