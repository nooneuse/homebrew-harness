"""Bounded JSON-RPC stdio transport with an owned process group.

Protocol sessions remain MCP SDK ClientSession; only process I/O is supervised here.
"""

import asyncio
import codecs
import os
import signal
import subprocess
from contextlib import asynccontextmanager

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage


class _PipeReceive(anyio.abc.ByteReceiveStream):
    def __init__(self, pipe):
        self.pipe = pipe
        os.set_blocking(pipe.fileno(), False)

    async def receive(self, max_bytes=65536):
        while True:
            await anyio.wait_readable(self.pipe.fileno())
            try:
                data = os.read(self.pipe.fileno(), max_bytes)
            except BlockingIOError:
                continue
            if not data:
                raise anyio.EndOfStream
            return data

    async def aclose(self):
        self.pipe.close()


class _PipeSend(anyio.abc.ByteSendStream):
    def __init__(self, pipe):
        self.pipe = pipe
        os.set_blocking(pipe.fileno(), False)

    async def send(self, data):
        while data:
            await anyio.wait_writable(self.pipe.fileno())
            try:
                sent = os.write(self.pipe.fileno(), data)
            except BlockingIOError:
                continue
            data = data[sent:]

    async def aclose(self):
        self.pipe.close()


async def _owned_thread(function, cancel=None):
    future = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.set()
        try:
            await asyncio.shield(future)
        except Exception:
            pass
        raise


@asynccontextmanager
async def controlled_stdio(spec, env, *, owner=None):
    incoming_send, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(0)
    process = None
    try:
        if owner is not None:
            process = await _owned_thread(owner.launch, owner.cancel)
            stdout, stdin, stderr = (
                _PipeReceive(process.stdout),
                _PipeSend(process.stdin),
                _PipeReceive(process.stderr),
            )
        else:
            process = await anyio.open_process(
                [spec.command, *spec.args],
                cwd=spec.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stdin, stderr = process.stdout, process.stdin, process.stderr
    except BaseException:
        if owner is not None:
            with anyio.CancelScope(shield=True):
                await _owned_thread(owner.settle)
        raise

    async def read():
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer = ""
        try:
            async with incoming_send:
                while True:
                    try:
                        chunk = await stdout.receive(65536)
                    except anyio.EndOfStream:
                        break
                    buffer += decoder.decode(chunk)
                    if len(buffer.encode("utf-8")) > 4 * 1024 * 1024:
                        raise ValueError("MCP frame exceeds transport limit.")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            await incoming_send.send(
                                SessionMessage(JSONRPCMessage.model_validate_json(line))
                            )
                if buffer.strip():
                    raise ValueError("MCP transport ended with an incomplete frame.")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    async def write():
        async with outgoing_receive:
            async for message in outgoing_receive:
                raw = (
                    message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode()
                    + b"\n"
                )
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("MCP outbound frame exceeds transport limit.")
                await stdin.send(raw)

    async def drain_stderr():
        # Service logging is not model data. Drain without buffering secrets or
        # unlimited temporary files; a noisy service cannot fill the host disk.
        try:
            while True:
                await stderr.receive(65536)
        except anyio.EndOfStream:
            pass

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(read)
            group.start_soon(write)
            group.start_soon(drain_stderr)
            try:
                yield incoming, outgoing
            finally:
                with anyio.CancelScope(shield=True):
                    # Always stop descendants, including after the server parent
                    # exits or closes its pipes. Do not await EOF without a limit.
                    if owner is not None:
                        await _owned_thread(owner.settle)
                    else:
                        for sig in (signal.SIGTERM, signal.SIGKILL):
                            try:
                                os.killpg(process.pid, sig)
                            except ProcessLookupError:
                                break
                        await process.wait()
                    group.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            for stream in (incoming, outgoing, incoming_send, outgoing_receive):
                await stream.aclose()
            if owner is None:
                await process.aclose()
