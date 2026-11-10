"""Platform-independent object identities, source pages and change feedback."""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Protocol

REFERENCE_ORIGIN = "platform_source"

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SUBJECTS = frozenset({"issue", "change_request"})
KINDS = SUBJECTS | {"comment", "review", "check"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def bounded(value, label, maximum=1000):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid " + label)
    return value


@dataclass(frozen=True)
class ObjectRef:
    provider: str
    instance: str
    project: str
    kind: str
    id: str
    number: int | None = field(default=None, compare=False)

    def __post_init__(self):
        for name in ("provider", "instance"):
            if not isinstance(getattr(self, name), str) or not NAME.fullmatch(
                getattr(self, name)
            ):
                raise ValueError("Invalid platform " + name)
        bounded(self.project, "project")
        bounded(self.id, "object identity", 200)
        if self.kind not in KINDS:
            raise ValueError("Unknown platform object kind.")
        if self.number is not None and (
            type(self.number) is not int or self.number < 1
        ):
            raise ValueError("Invalid display number.")

    def data(self):
        return asdict(self)

    def key(self):
        return digest([self.provider, self.instance, self.project, self.kind, self.id])

    def scope(self):
        return self.provider, self.instance, self.project


@dataclass(frozen=True)
class SourceEvent:
    object: ObjectRef
    subject: ObjectRef
    version: str
    content: dict

    def __post_init__(self):
        if not isinstance(self.object, ObjectRef) or not isinstance(
            self.subject, ObjectRef
        ):
            raise ValueError("Source requires typed object references.")
        if (
            self.object.scope() != self.subject.scope()
            or self.subject.kind not in SUBJECTS
        ):
            raise ValueError("Source event crosses its project or subject scope.")
        bounded(self.version, "object version", 100)
        if not isinstance(self.content, dict) or set(self.content) - {
            "title",
            "body",
            "state",
            "actor_id",
            "metadata",
        }:
            raise ValueError("Invalid normalized source content.")
        for name in ("title", "body"):
            if not isinstance(self.content.get(name, ""), str):
                raise ValueError("Source text must be a string.")
        if len(json.dumps(self.content, ensure_ascii=False, allow_nan=False)) > 64000:
            raise ValueError("Source content exceeds its limit.")
        if self.object.kind in SUBJECTS and self.object != self.subject:
            raise ValueError("Subject update must identify its own subject.")

    def data(self):
        value = {
            "object": self.object.data(),
            "subject": self.subject.data(),
            "version": self.version,
            "content": self.content,
        }
        return {
            **value,
            "key": digest(
                [self.object.key(), self.subject.key(), self.version, self.content]
            ),
        }


@dataclass(frozen=True)
class SourcePage:
    events: tuple[SourceEvent, ...]
    cursor: dict
    next_poll: float
    state: str

    def validate(self, scope):
        if not isinstance(self.events, (tuple, list)) or len(self.events) > 100:
            raise ValueError("Source page exceeds its event limit.")
        if (
            not isinstance(self.cursor, dict)
            or len(json.dumps(self.cursor, allow_nan=False)) > 64000
        ):
            raise ValueError("Invalid opaque source cursor.")
        if (
            type(self.next_poll) not in {int, float}
            or not math.isfinite(self.next_poll)
            or self.next_poll < 0
        ):
            raise ValueError("Invalid source poll time.")
        if self.state not in {"ready", "reconciling", "rescan_required"}:
            raise ValueError("Invalid source page state.")
        for event in self.events:
            if not isinstance(event, SourceEvent) or event.subject.scope() != scope:
                raise ValueError(
                    "Source page targets a different platform/instance/project."
                )
        return self


@dataclass(frozen=True)
class FeedbackEvent:
    object: ObjectRef
    actor_id: int | None
    actor_kind: str
    head: str | None
    version: str
    body: str | dict
    status: str | None
    conclusion: str | None
    pending: bool = False


@dataclass(frozen=True)
class ChangeFeedback:
    subject: ObjectRef
    head: str
    events: tuple[FeedbackEvent, ...]
    cursor: dict

    def validate(self, subject):
        if self.subject != subject or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", self.head
        ):
            raise ValueError("Invalid change feedback subject or head.")
        if (
            not isinstance(self.cursor, dict)
            or len(json.dumps(self.cursor, allow_nan=False)) > 64000
        ):
            raise ValueError("Invalid feedback cursor.")
        if not isinstance(self.events, (tuple, list)) or len(self.events) > 30000:
            raise ValueError("Invalid feedback batch size.")
        for event in self.events:
            if (
                not isinstance(event, FeedbackEvent)
                or event.object.scope() != subject.scope()
                or event.object.kind not in {"comment", "review", "check"}
            ):
                raise ValueError("Feedback event crosses its scope.")
            if (
                event.actor_kind not in {"user", "app"}
                or type(event.pending) is not bool
            ):
                raise ValueError("Invalid feedback actor or pending state.")
            if event.actor_id is not None and (
                type(event.actor_id) is not int or event.actor_id < 1
            ):
                raise ValueError("Invalid feedback actor ID.")
            if (
                not isinstance(event.body, (str, dict))
                or event.head is not None
                and (
                    not isinstance(event.head, str)
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", event.head)
                )
            ):
                raise ValueError("Invalid feedback body or revision.")
            for value in (event.status, event.conclusion):
                if value is not None and (
                    not isinstance(value, str) or len(value) > 100
                ):
                    raise ValueError("Invalid feedback status.")
            if (
                not isinstance(event.version, str)
                or len(json.dumps(asdict(event), ensure_ascii=False, allow_nan=False))
                > 64000
            ):
                raise ValueError("Feedback content exceeds its limit.")
        return self


class RepositoryAdapter(Protocol):
    def poll(self, position: dict, deadline) -> SourcePage: ...
    def close(self): ...


class ChangeRequestAdapter(Protocol):
    def fetch(self, subject: ObjectRef, position: dict, deadline) -> ChangeFeedback: ...
    def close(self): ...
