"""Host model selection shared by session and direct execution entry points."""

import io
import os
import stat
from pathlib import Path

from homebrew_harness.security.data_boundary import register_private_values
from .model_registry import ModelRegistry, load_model_registry


def read_environment(path, workspace, *, private=False):
    from dotenv import dotenv_values

    path = Path(path).expanduser()
    if private and (
        path.is_symlink() or path.resolve().is_relative_to(Path(workspace).resolve())
    ):
        raise ValueError(
            "Model environment must be a private host file outside the workspace."
        )
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or private
            and (info.st_uid != os.getuid() or info.st_mode & 0o077)
        ):
            raise ValueError(
                "Model environment must be a regular owner-only host file."
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Model environment exceeds 64 KiB.")
        values = dotenv_values(
            stream=io.StringIO(raw.decode("utf-8")), interpolate=False
        )
    except FileNotFoundError:
        if private:
            raise ValueError("Private model environment is unavailable.") from None
        values = {}
    except (OSError, UnicodeError) as error:
        raise ValueError(
            "Cannot read model environment (" + type(error).__name__ + ")."
        ) from error
    finally:
        if fd is not None:
            os.close(fd)
    return {**values, **os.environ}


class _ClientContext:
    def __init__(self, raw, entered):
        self.raw, self.entered = raw, entered

    def close(self):
        if self.entered:
            self.raw.__exit__(None, None, None)
        elif hasattr(self.raw, "close"):
            self.raw.close()


class HostModels:
    def __init__(
        self, workdir, *, config=None, alias=None, environment=None, client_factory=None
    ):
        self.environment = dict(os.environ if environment is None else environment)
        self.registry = (
            load_model_registry(
                config,
                workdir,
                client_factory=client_factory,
                environment=self.environment,
            )
            if config
            else ModelRegistry(client_factory=client_factory)
        )
        self.alias = alias if alias is not None else self.registry.lead_default
        self.factory, self.binding = client_factory, None
        self.gateways = []
        from homebrew_harness.runtime.runtime_control import Shutdown

        self.shutdown = Shutdown()
        if self.alias is not None:
            self.registry.validate(self.alias)
        else:
            missing = [
                name
                for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
                if not str(self.environment.get(name) or "").strip()
            ]
            if missing:
                raise ValueError("Missing configuration: " + ", ".join(missing))
            register_private_values(
                self.environment["OPENAI_API_KEY"], self.environment["OPENAI_BASE_URL"]
            )

    def open(self, *, context_chars=50000, output_tokens=4096):
        if self.binding is not None:
            return self.binding
        client = model = None
        if self.alias is None:
            model = self.environment["OPENAI_MODEL"]
            if self.factory is None:
                from homebrew_harness.execution.providers.adapter import ProtocolAdapter

                client = ProtocolAdapter(
                    "chat_completions",
                    api_key=self.environment["OPENAI_API_KEY"],
                    base_url=self.environment["OPENAI_BASE_URL"],
                    transport=self.environment.get("HARNESS_MODEL_TRANSPORT", "auto"),
                    expected_transport=self.registry.backend_pins.get("@inherited"),
                )
                self.registry.own_client(client)
            else:
                raw = self.factory(
                    api_key=self.environment["OPENAI_API_KEY"],
                    base_url=self.environment["OPENAI_BASE_URL"],
                    timeout=60,
                    max_retries=0,
                )
                entered = hasattr(raw, "__enter__")
                self.registry.own_client(_ClientContext(raw, entered))
                client = raw.__enter__() if entered else raw
        self.binding = self.registry.select(
            self.alias,
            client=client,
            model=model,
            context_chars=context_chars,
            max_output_tokens=output_tokens,
        )
        return self.binding

    def track_gateway(self, gateway):
        self.gateways.append(gateway)

    def close_when_idle(self, timeout=0.2, *, wait=None):
        def finish():
            if wait is not None:
                wait()
            for gateway in self.gateways:
                gateway.wait_idle(None)
            self.registry.close()

        return self.shutdown.request(finish, timeout)

    def close(self):
        self.registry.close()
