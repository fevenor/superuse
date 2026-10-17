"""The unique serial actor: one writer thread, priority control channel.

Everything that touches the serial port goes through this actor (DESIGN.md
§3). Reports are generation-checked *when the writer thread dequeues them*,
and a fence is processed on the same thread, so authorization decisions and
writes are ordered through a single executor — the TOCTOU window the state
machine requires closed. The fence's two neutralization reports are the only
writes allowed to cross owners.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable

from superuse.core.models import KEYBOARD_RELEASE, MOUSE_ZERO_RELEASE
from superuse.core.ports import AuthorizedReport, FenceResult, FenceTicket, WriteOutcome
from superuse.device.protocol import (
    EXT_INFO_KIND,
    ExtFrameParser,
    build_device_info_query,
    encode_hid_report,
    parse_device_info,
)
from superuse.device.transport import SerialTransport, TransportClosed

logger = logging.getLogger(__name__)

PRIORITY_CONTROL = 0
PRIORITY_INPUT = 1


class SerialActor:
    """Real actor used for both real and simulated transports.

    ``transport_factory`` returns an opened-on-start transport. No hardware is
    touched until :meth:`start`.
    """

    def __init__(
        self,
        transport_factory: Callable[[], SerialTransport],
        *,
        loop: asyncio.AbstractEventLoop,
        write_timeout_s: float = 0.25,
        input_queue_limit: int = 128,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self._transport_factory = transport_factory
        self._loop = loop
        self._write_timeout_s = write_timeout_s
        self._input_queue_limit = input_queue_limit
        self._on_status = on_status

        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._generation = 0  # written only on the writer thread
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False
        self._status = "starting"
        self.device_info: dict | None = None
        self._transport: SerialTransport | None = None
        self._pending_inputs = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    def start(self, *, query_device_info: bool = True) -> None:
        self._transport = self._transport_factory()
        try:
            self._transport.open()
        except Exception:
            self._set_status("fault")
            raise
        self._set_status("ready")
        self._writer = threading.Thread(target=self._writer_loop, name="serial-writer", daemon=True)
        self._reader = threading.Thread(target=self._reader_loop, name="serial-reader", daemon=True)
        self._writer.start()
        self._reader.start()
        if query_device_info:
            # One read-only query at init/recovery via the same controlled
            # path (docs/development/device-video.md §3); failure is tolerated.

            def _done(outcome: WriteOutcome) -> None:
                logger.debug("device info query outcome: %s", outcome.error or "ok")

            self._enqueue_control(("raw", build_device_info_query(), _done))

    def stop(self) -> None:
        self._stopping = True
        self._enqueue_control(("stop", None, None))
        for thread in (self._writer, self._reader):
            if thread is not None:
                thread.join(timeout=2.0)
        self._writer = self._reader = None
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._set_status("disconnected")

    def _set_status(self, status: str) -> None:
        self._status = status
        if self._on_status is not None:
            callback = self._on_status
            try:
                self._loop.call_soon_threadsafe(callback, status)
            except RuntimeError:  # loop closed during shutdown
                pass

    def _enqueue_control(self, payload) -> None:
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._queue.put((PRIORITY_CONTROL, seq, payload))

    # ------------------------------------------------------------------
    # ActorPort surface (called on the loop thread)
    # ------------------------------------------------------------------

    def submit(self, report: AuthorizedReport):
        future: asyncio.Future[WriteOutcome] = self._loop.create_future()
        if self._stopping or self._status in ("fault", "disconnected"):
            future.set_result(WriteOutcome(0, "CLOSED"))
            return future
        if not report.priority:
            if self._pending_inputs >= self._input_queue_limit:
                future.set_result(WriteOutcome(0, "BACKPRESSURE"))
                return future
            self._pending_inputs += 1

        def _release_slot(future: asyncio.Future) -> None:
            if not report.priority:
                self._pending_inputs -= 1

        future.add_done_callback(_release_slot)
        self._enqueue_input(("report", report, future))
        return future

    async def fence(self, ticket: FenceTicket, timeout_s: float) -> FenceResult:
        if self._writer is None or not self._writer.is_alive():
            return FenceResult(ok=False, error="WRITE_FAILED")
        done = threading.Event()
        holder: dict[str, FenceResult] = {}
        self._enqueue_control(("fence", ticket, timeout_s, done, holder))
        loop = asyncio.get_running_loop()
        try:
            signalled = await asyncio.wait_for(
                loop.run_in_executor(None, done.wait, timeout_s), timeout_s + 0.5
            )
        except TimeoutError:
            return FenceResult(ok=False, error="TIMEOUT")
        if not signalled:
            return FenceResult(ok=False, error="TIMEOUT")
        return holder.get("result", FenceResult(ok=False, error="WRITE_FAILED"))

    def _enqueue_input(self, payload) -> None:
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._queue.put((PRIORITY_INPUT, seq, payload))

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            _, _, payload = self._queue.get()
            try:
                kind = payload[0]
                if kind == "stop":
                    return
                if kind == "report":
                    self._process_report(payload)
                elif kind == "fence":
                    self._process_fence(payload)
                elif kind == "raw":
                    self._process_raw(payload)
            except Exception:  # pragma: no cover - never kill the writer
                logger.exception("serial writer item failed")
            finally:
                self._queue.task_done()

    def _complete(self, future: asyncio.Future, outcome: WriteOutcome) -> None:
        def _resolve() -> None:
            if not future.done():
                future.set_result(outcome)

        try:
            self._loop.call_soon_threadsafe(_resolve)
        except RuntimeError:  # pragma: no cover - loop gone during shutdown
            pass

    def _write_bytes(self, data: bytes) -> int:
        transport = self._transport
        if transport is None:
            raise TransportClosed("actor not started")
        n = transport.write_report(data, self._write_timeout_s)
        if n < len(data):
            raise ShortWrite(n)
        return n

    def _process_report(self, payload) -> None:
        _, report, future = payload
        if report.generation < self._generation:
            self._complete(future, WriteOutcome(0, "REVOKED"))
            return
        try:
            data = encode_hid_report(report.report)
        except Exception:
            self._complete(future, WriteOutcome(0, "FAILED"))
            return
        try:
            n = self._write_bytes(data)
            self._complete(future, WriteOutcome(n, None))
        except TimeoutError:
            self._set_status("fault")
            self._complete(future, WriteOutcome(0, "TIMEOUT"))
        except ShortWrite as exc:
            self._set_status("fault")
            self._complete(future, WriteOutcome(int(exc.args[0]), "SHORT_WRITE"))
        except TransportClosed:
            self._set_status("disconnected")
            self._complete(future, WriteOutcome(0, "CLOSED"))
        except Exception:
            self._set_status("fault")
            self._complete(future, WriteOutcome(0, "FAILED"))

    def _process_fence(self, payload) -> None:
        _, ticket, timeout_s, done, holder = payload
        self._generation = ticket.generation
        try:
            self._write_bytes(encode_hid_report(KEYBOARD_RELEASE))
            self._write_bytes(encode_hid_report(MOUSE_ZERO_RELEASE))
            holder["result"] = FenceResult(
                ok=True,
                neutral_reports=(KEYBOARD_RELEASE, MOUSE_ZERO_RELEASE),
            )
        except TimeoutError:
            self._set_status("fault")
            holder["result"] = FenceResult(ok=False, error="TIMEOUT")
        except Exception:
            self._set_status("fault")
            holder["result"] = FenceResult(ok=False, error="WRITE_FAILED")
        finally:
            done.set()

    def _process_raw(self, payload) -> None:
        _, data, done = payload
        try:
            self._write_bytes(data)
            done(WriteOutcome(len(data), None))
        except Exception as exc:
            done(WriteOutcome(0, f"FAILED:{exc}"))

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        parser = ExtFrameParser()
        while not self._stopping:
            transport = self._transport
            if transport is None:
                return
            try:
                chunk = transport.read(0.1)
            except TransportClosed:
                self._set_status("disconnected")
                return
            except Exception:  # pragma: no cover - defensive
                logger.exception("serial reader error")
                self._set_status("fault")
                return
            if not chunk:
                continue
            for frame in parser.feed(chunk):
                if frame.checksum_ok and frame.kind == EXT_INFO_KIND:
                    self.device_info = parse_device_info(frame.payload)


class ShortWrite(Exception):
    pass
