"""Bounded legacy echo and held-session receiver on the existing TCP test port."""
import asyncio
import contextlib
import json
from pathlib import Path
import struct
import threading
import time

FRAME = struct.Struct("!8sQIIQ")
MAGIC = b"NGFWHOLD"
LEGACY = b"ngfw-lab\n"


class Receiver:
    def __init__(self, host="0.0.0.0", port=9000, state_path="/tmp/ngfw-held-server.json",
                 max_connections=6000, max_runs=64, idle_timeout=30):
        self.host, self.port = host, port
        self.path = Path(state_path)
        self.max_connections, self.max_runs = max_connections, max_runs
        self.idle_timeout = idle_timeout
        self.error = None
        self.loop = self.stop = None
        self.ready = threading.Event()
        self.state = dict(kind="held-session-receiver", active=0, peak=0,
                          messages=0, errors=0, runs={})
        self.writers, self.tasks, self.seen = set(), set(), {}
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("TCP receiver startup deadline exceeded")
        if self.error:
            raise RuntimeError(self.error)

    def _publish(self, status):
        value = dict(self.state, monotonic=time.monotonic(),
                     wall_time=time.time(), status=status, port=self.port)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.path)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        run = None
        if len(self.writers) >= self.max_connections:
            writer.close()
            self.tasks.discard(task)
            return
        self.writers.add(writer)
        try:
            first = await asyncio.wait_for(reader.readexactly(8), min(2, self.idle_timeout))
            if first == LEGACY[:8]:
                tail = await asyncio.wait_for(reader.readexactly(1), 2)
                if first + tail == LEGACY:
                    writer.write(LEGACY)
                    await asyncio.wait_for(writer.drain(), 2)
                return
            if first != MAGIC:
                raise ValueError("unknown TCP test protocol")
            seq_previous = -1
            connection_id = None
            while True:
                rest = await asyncio.wait_for(reader.readexactly(FRAME.size - 8), self.idle_timeout)
                data = first + rest
                magic, ident, cid, seq, stamp = FRAME.unpack(data)
                if magic != MAGIC or cid >= self.max_connections or seq != seq_previous + 1:
                    raise ValueError("invalid bounded held-session frame")
                if run is None:
                    key = str(ident)
                    if key not in self.state["runs"]:
                        if len(self.state["runs"]) >= self.max_runs:
                            raise ValueError("held-session run limit")
                        self.state["runs"][key] = dict(active=0, peak=0, messages=0, connections=0)
                        self.seen[key] = set()
                    if cid in self.seen[key]:
                        raise ValueError("duplicate held-session connection id")
                    self.seen[key].add(cid)
                    run, connection_id = key, cid
                    row = self.state["runs"][run]
                    row["active"] += 1
                    row["connections"] += 1
                    row["peak"] = max(row["peak"], row["active"])
                    self.state["active"] += 1
                    self.state["peak"] = max(self.state["peak"], self.state["active"])
                elif str(ident) != run or cid != connection_id:
                    raise ValueError("held-session identity changed")
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 2)
                self.state["messages"] += 1
                self.state["runs"][run]["messages"] += 1
                seq_previous = seq
                first = await asyncio.wait_for(reader.readexactly(8), self.idle_timeout)
        except asyncio.IncompleteReadError:
            pass
        except (OSError, ValueError, TimeoutError):
            self.state["errors"] += 1
        finally:
            if run is not None:
                self.state["active"] -= 1
                self.state["runs"][run]["active"] -= 1
            self.writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        listener = await asyncio.start_server(self._handle, self.host, self.port,
                                             backlog=128, limit=1024)
        self.port = listener.sockets[0].getsockname()[1]
        try:
            self._publish("running")
            self.ready.set()
            while not self.stop.is_set():
                self._publish("running")
                try:
                    await asyncio.wait_for(self.stop.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            listener.close()
            await listener.wait_closed()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            self._publish("stopped")

    def _thread(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            self.ready.set()

    def close(self):
        if self.loop and self.stop and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("TCP receiver shutdown deadline exceeded")