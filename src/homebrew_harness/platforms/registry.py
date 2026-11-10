"""Explicit host platform catalog; adapters alone interpret project and API details."""

import hashlib
import re
import ssl
from pathlib import Path
import os
import stat

from .contracts import NAME, SourcePage, digest
from .http import JsonTransport

from .github.repository import GithubRepository, validate_project
from .github.feedback import GithubFeedback
from .github.delivery import GithubDelivery
from .github.communication import GithubCommunication

PROVIDERS = {
    "github": {
        "project": validate_project,
        "repository_events": GithubRepository,
        "change_request_feedback": GithubFeedback,
        "delivery": GithubDelivery,
        "communication": GithubCommunication,
    }
}


def catalog():
    return [
        {"provider": name, "capabilities": sorted(k for k in entries if k != "project")}
        for name, entries in sorted(PROVIDERS.items())
    ]


def validate_declaration(raw, capability, *, named=False):
    if not isinstance(raw, dict):
        raise ValueError("Platform declaration must be an object.")
    common = {
        "provider",
        "instance",
        "project",
        "api_url_env",
        "token_env",
        "ca_file_env",
        "poll_seconds",
        "max_pages",
    }
    extra = {"name"} if named else {"number", "actor_ids", "app_ids"}
    if set(raw) - common - extra:
        raise ValueError("Unknown platform declaration fields.")
    provider = raw.get("provider")
    if (
        not isinstance(provider, str)
        or provider not in PROVIDERS
        or capability not in PROVIDERS[provider]
    ):
        raise ValueError("Provider does not implement the requested capability.")
    for field in ("instance",):
        if not isinstance(raw.get(field), str) or not NAME.fullmatch(raw[field]):
            raise ValueError("Invalid platform instance alias.")
    if named and (
        not isinstance(raw.get("name"), str) or not NAME.fullmatch(raw["name"])
    ):
        raise ValueError("Invalid source name.")
    for field in ("api_url_env", "token_env", "ca_file_env"):
        value = raw.get(field)
        if field == "ca_file_env" and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,100}", value
        ):
            raise ValueError("Platform connections require environment references.")
    project = PROVIDERS[provider]["project"](raw.get("project"))
    result = {
        "poll_seconds": 43200 if named else 30,
        "max_pages": 5 if named else 10,
        **raw,
        "project": project,
    }
    import math

    for field, maximum in (
        ("poll_seconds", 31536000 if named else 3600),
        ("max_pages", 100),
    ):
        value = result[field]
        if (
            type(value) not in ({int} if field == "max_pages" else {int, float})
            or not math.isfinite(value)
            or not (1 if field == "max_pages" else 0.1) <= value <= maximum
        ):
            raise ValueError("Invalid platform polling bounds.")
    if not named:
        if type(result.get("number")) is not int or result["number"] < 1:
            raise ValueError("Change request requires a positive project number.")
        for field in ("actor_ids", "app_ids"):
            if not isinstance(result.get(field), (list, tuple)) or any(
                type(v) is not int or v < 1 for v in result[field]
            ):
                raise ValueError(
                    "Feedback actor IDs must be positive stable identities."
                )
    return result


def connection(spec, environment, roots=()):
    endpoint = environment.get(spec["api_url_env"])
    token = environment.get(spec["token_env"])
    if not endpoint or not token:
        raise ValueError("Platform host credentials are missing.")
    verify = True
    cert_hash = None
    if spec.get("ca_file_env"):
        value = environment.get(spec["ca_file_env"])
        if not value:
            raise ValueError("Platform CA configuration is missing.")
        path = Path(value).expanduser()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or any(p.is_symlink() for p in path.parents)
            or any(path.resolve().is_relative_to(Path(r).resolve()) for r in roots)
        ):
            raise ValueError(
                "Platform CA must be a host file outside mutable workspaces."
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 1_000_000:
                raise ValueError("Platform CA must be a bounded regular file.")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(1_000_001)
            after = os.stat(path, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("Platform CA changed while reading.")
        finally:
            os.close(fd)
        if len(raw) > 1_000_000:
            raise ValueError("Platform CA file exceeds its limit.")
        try:
            verify = ssl.create_default_context(cadata=raw.decode("ascii"))
        except (ValueError, UnicodeError, ssl.SSLError):
            raise ValueError("Invalid platform CA certificate.") from None
        cert_hash = hashlib.sha256(raw).hexdigest()
    transport = JsonTransport(endpoint, token, verify=verify)
    return transport, digest({"endpoint": transport.identity, "certificate": cert_hash})


class RepositorySource:
    def __init__(self, declaration, environment, *, roots=()):
        self.spec = validate_declaration(declaration, "repository_events", named=True)
        transport, identity = connection(self.spec, environment, roots)
        try:
            self.adapter = PROVIDERS[self.spec["provider"]]["repository_events"](
                self.spec, transport
            )
        except BaseException:
            transport.close()
            raise
        self.scope = (
            self.spec["provider"],
            self.spec["instance"],
            self.spec["project"],
        )
        self.identity = digest(
            {"declaration": self.spec, "connection": identity, "adapter_version": 1}
        )

    def close(self):
        self.adapter.close()

    def poll(self, saved, deadline):
        if not isinstance(saved, dict):
            raise ValueError("Invalid bound source cursor.")
        if saved and (
            set(saved) != {"binding", "position"} or saved["binding"] != self.identity
        ):
            raise ValueError("Source cursor belongs to another platform binding.")
        position = saved.get("position", {})
        page = self.adapter.poll(position, deadline)
        if not isinstance(page, SourcePage):
            raise ValueError("Adapter returned an invalid source page.")
        page.validate(self.scope)
        return SourcePage(
            page.events,
            {"binding": self.identity, "position": page.cursor},
            page.next_poll,
            page.state,
        )


def change_request(declaration, environment, *, roots=()):
    spec = validate_declaration(declaration, "change_request_feedback")
    transport, identity = connection(spec, environment, roots)
    try:
        adapter = PROVIDERS[spec["provider"]]["change_request_feedback"](
            spec, transport
        )
    except BaseException:
        transport.close()
        raise
    return (
        spec,
        adapter,
        digest({"declaration": spec, "connection": identity, "adapter_version": 1}),
    )
