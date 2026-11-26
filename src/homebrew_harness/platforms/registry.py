"""Explicit host platform catalog; adapters alone interpret project and API details."""

import hashlib
import re
import ssl
from pathlib import Path
import os
import stat

from .contracts import NAME, SourcePage, digest
from .http import JsonTransport
from homebrew_harness.runtime.limits import current_limits, positive

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


def read_credentials_file(path, roots=(), *, label="Platform credentials", error=None):
    fail = error or ValueError
    target = Path(path).expanduser()
    if (
        not target.is_absolute()
        or target.is_symlink()
        or any(parent.is_symlink() for parent in target.parents)
        or any(target.resolve().is_relative_to(Path(root).resolve()) for root in roots)
    ):
        raise fail(f"{label} must be an external host file without symlinks.")
    fd = None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise fail(f"{label} must be an owner-only regular host file.")
        limit = current_limits().config_file_bytes
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise fail(f"{label} file exceeds its size limit.")
        import io
        from dotenv import dotenv_values

        return dict(
            dotenv_values(stream=io.StringIO(raw.decode("utf-8")), interpolate=False)
        )
    except FileNotFoundError:
        raise fail(f"{label} file is unavailable.") from None
    except (OSError, UnicodeError) as exc:
        raise fail(f"Cannot read {label.lower()} ({type(exc).__name__}).") from exc
    finally:
        if fd is not None:
            os.close(fd)


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
        "request_seconds",
    }
    extra = {"name"} if named else {"number", "actor_ids", "app_ids"}
    if capability != "communication":
        extra = extra | {"credentials_file"}
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
    credentials_file = raw.get("credentials_file")
    if credentials_file is not None:
        if not isinstance(credentials_file, str) or not credentials_file.strip():
            raise ValueError(
                "Platform credentials file must be an absolute external path."
            )
        cred_path = Path(credentials_file).expanduser()
        if not cred_path.is_absolute():
            raise ValueError(
                "Platform credentials file must be an absolute external path."
            )
        credentials_file = str(cred_path)
    project = PROVIDERS[provider]["project"](raw.get("project"))
    result = {
        "poll_seconds": 43200 if named else 30,
        "max_pages": 5 if named else 10,
        "request_seconds": current_limits().platform_request_seconds,
        **raw,
        "project": project,
    }
    if credentials_file is None:
        result.pop("credentials_file", None)
    else:
        result["credentials_file"] = credentials_file
    import math
    positive(result["request_seconds"], "platform.request_seconds")

    for field in ("poll_seconds", "max_pages"):
        value = result[field]
        if (
            type(value) not in ({int} if field == "max_pages" else {int, float})
            or not math.isfinite(value)
            or value < (1 if field == "max_pages" else 0.1)
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


def load_ca_bundle(ca_file, roots=()):
    import tempfile

    path = Path(ca_file).expanduser()
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
        with tempfile.TemporaryDirectory(prefix="harness-ca-") as tmp_dir:
            os.chmod(tmp_dir, 0o700)
            tmp_file = os.path.join(tmp_dir, "bundle.pem")
            out_fd = os.open(
                tmp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(out_fd, "wb", closefd=False) as out:
                    out.write(raw)
            finally:
                os.close(out_fd)
            verify = ssl.create_default_context(cafile=tmp_file)
    except (ValueError, OSError, ssl.SSLError):
        raise ValueError("Invalid platform CA certificate.") from None
    return verify, hashlib.sha256(raw).hexdigest()


def transport_binding(transport):
    return (transport.endpoint.rstrip("/"), getattr(transport, "certificate_hash", None))


def check_instance_binding(instances, provider, instance, binding):
    key = (provider, instance)
    expected = instances.get(key)
    if expected is not None and expected != binding:
        raise ValueError(
            "Source and publisher must use the same declared platform instance."
        )
    instances[key] = binding


def connection(spec, environment, roots=()):
    values = (
        read_credentials_file(
            spec["credentials_file"], roots=roots, label="Platform credentials"
        )
        if spec.get("credentials_file")
        else environment
    )
    endpoint = values.get(spec["api_url_env"])
    token = values.get(spec["token_env"])
    if not endpoint or not token:
        raise ValueError("Platform host credentials are missing.")
    verify = True
    cert_hash = None
    if spec.get("ca_file_env"):
        value = values.get(spec["ca_file_env"])
        if not value:
            raise ValueError("Platform CA configuration is missing.")
        verify, cert_hash = load_ca_bundle(value, roots=roots)
    transport = JsonTransport(endpoint, token, verify=verify)
    transport.certificate_hash = cert_hash
    return transport, digest({"endpoint": transport.identity, "certificate": cert_hash})


class RepositorySource:
    def __init__(self, declaration, environment, *, roots=()):
        self.spec = validate_declaration(declaration, "repository_events", named=True)
        transport, identity = connection(self.spec, environment, roots)
        self.connection_binding = transport_binding(transport)
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
            {"declaration": {k:v for k,v in self.spec.items() if k not in {"poll_seconds","max_pages","request_seconds"}}, "connection": identity, "adapter_version": 1}
        )
        self.legacy_identity = digest({"declaration": {k:v for k,v in self.spec.items() if k != "request_seconds"},
                                       "connection": identity, "adapter_version": 1})

    def close(self):
        self.adapter.close()

    def poll(self, saved, deadline):
        if not isinstance(saved, dict):
            raise ValueError("Invalid bound source cursor.")
        if saved and (
            set(saved) != {"binding", "position"} or saved["binding"] not in {self.identity, self.legacy_identity}
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
