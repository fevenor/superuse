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

from superuse.core.models import KEYBOARD_RELEASE, MOUSE_ZERO_RELEASE, MouseAbsReport
from superuse.core.ports import AuthorizedReport, FenceResult, FenceTicket, WriteOutcome
from superuse.device.protocol import (
    EXT_GENERAL_REPLY_KIND,
    EXT_INFO_KIND,
    EXT_QUERY_CHANNEL,
    MOUSE_MODE_RELATIVE,
    ExtFrameParser,
    build_device_info_query,
    build_device_restart_frame,
    build_mouse_mode_frame,
    encode_hid_report,
    parse_device_info,
    parse_general_reply,
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
        on_device_info: Callable[[dict], None] | None = None,
    ) -> None:
        self._transport_factory = transport_factory
        self._loop = loop
        self._write_timeout_s = write_timeout_s
        self._input_queue_limit = input_queue_limit
        self._on_status = on_status
        self._on_device_info = on_device_info

        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._generation = 0  # written only on the writer thread
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False
        self._status = "starting"
        self.device_info: dict | None = None
        # Raw mouse coordinate mode from the last device-info reply (reader
        # thread writes, writer thread reads for the final report gate).
        self._mouse_mode_raw: int | None = None
        # Single-slot correlation for the pending extension-write general
        # reply (created by the loop thread, filled by the reader thread);
        # device-control writes are serialized at the broker level.
        self._pending_ext_reply: dict | None = None
        # Last absolute position written (writer thread): the fence's
        # absolute-mode neutralization lifts buttons without moving here.
        self._last_abs_x = 2048
        self._last_abs_y = 2048
        self._transport: SerialTransport | None = None
        self._pending_inputs = 0
        self._heartbeat: object | None = None

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
        self._heartbeat = self._loop.call_later(5.0, self._heartbeat_tick)
        if query_device_info:
            # One read-only query at init/recovery via the same controlled
            # path (docs/development/device-video.md §3); failure is tolerated.

            def _done(outcome: WriteOutcome) -> None:
                logger.debug("device info query outcome: %s", outcome.error or "ok")

            self._enqueue_control(("raw", build_device_info_query(), _done))

    def _heartbeat_tick(self) -> None:
        """Vendor-parity 5s keepalive: the webapp re-queries device info every
        5s; skipping it correlates with the device dropping HID forwarding in
        idle sessions. Also keeps diagnostics (mode/temperature) fresh."""
        try:
            if not self._stopping and self._status == "ready":
                self._enqueue_control(("raw", build_device_info_query(), lambda outcome: None))
        finally:
            if not self._stopping:
                try:
                    self._heartbeat = self._loop.call_later(5.0, self._heartbeat_tick)
                except RuntimeError:  # pragma: no cover - loop closed
                    pass

    def stop(self) -> None:
        self._stopping = True
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
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

    async def _control_write(self, item: tuple, *, reply_timeout_s: float) -> WriteOutcome:
        """One device-control extension write with best-effort rsp matching.

        The pending-reply slot is created before the write is queued, so the
        reader thread can park the device's kind-0 general reply even if it
        lands before the loop resumes here. rsp_code 0 (or a silent timeout)
        returns ok; a non-zero rsp_code returns ``REJECTED:<code>``.
        """
        _, _, future = item
        if self._stopping or self._status in ("fault", "disconnected"):
            return WriteOutcome(0, "CLOSED")
        pending: dict = {"event": threading.Event(), "rsp_code": None, "app_id": None}
        self._pending_ext_reply = pending
        self._enqueue_control(item)
        outcome = await future
        try:
            if outcome.ok and reply_timeout_s > 0:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(None, pending["event"].wait, reply_timeout_s),
                    reply_timeout_s + 0.5,
                )
                rsp_code = pending["rsp_code"]
                if rsp_code not in (None, 0):
                    return WriteOutcome(0, f"REJECTED:{rsp_code}")
        except TimeoutError:
            pass  # no reply in time: stay optimistic (restart may kill the port)
        finally:
            self._pending_ext_reply = None
        return outcome

    async def set_mouse_mode(self, mode: int, *, reply_timeout_s: float = 0.5) -> WriteOutcome:
        """User-authorized coordinate-mode write on the writer thread.

        The stored setting changes immediately, but the mouse HID interface
        only rebinds after a device restart (see :meth:`restart_device`); the
        device-info query is re-sent after every accepted write so the reader
        refreshes ``device_info`` and the reported mode follows the change.
        """
        return await self._control_write(
            ("mouse_mode", build_mouse_mode_frame(mode), self._loop.create_future()),
            reply_timeout_s=reply_timeout_s,
        )

    async def restart_device(self, *, reply_timeout_s: float = 0.3) -> WriteOutcome:
        """User-authorized restartDevice write on the writer thread.

        The reply is best-effort: the device may drop the CDC connection
        before answering, so a missing rsp is not an error. The caller is
        expected to cycle the serial connection afterwards.
        """
        return await self._control_write(
            ("device_restart", build_device_restart_frame(), self._loop.create_future()),
            reply_timeout_s=reply_timeout_s,
        )

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
                elif kind == "mouse_mode":
                    self._process_mouse_mode(payload)
                elif kind == "device_restart":
                    self._process_device_restart(payload)
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
        if (
            isinstance(report.report, MouseAbsReport)
            and self._mouse_mode_raw == MOUSE_MODE_RELATIVE
        ):
            # Final executor-side gate: the device silently drops absolute
            # reports while its persisted coordinate mode is relative, so a
            # queued report must not be written (submission checks come first,
            # but this is the authoritative ordering point).
            self._complete(future, WriteOutcome(0, "MOUSE_MODE"))
            return
        try:
            data = encode_hid_report(report.report)
        except Exception:
            self._complete(future, WriteOutcome(0, "FAILED"))
            return
        try:
            n = self._write_bytes(data)
            if isinstance(report.report, MouseAbsReport):
                self._last_abs_x = report.report.x
                self._last_abs_y = report.report.y
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
        # Hardware evidence (2026-10-19 hm11): while the device is in absolute
        # mode a zero 0x05 neither moves nor RELEASES the pointer, and vice
        # versa in relative mode — so both zero reports are written; each is a
        # harmless no-op in the other mode. The absolute lift stays at the
        # last written position so the barrier never teleport the pointer.
        abs_zero = MouseAbsReport(0, self._last_abs_x, self._last_abs_y, 0)
        try:
            self._write_bytes(encode_hid_report(KEYBOARD_RELEASE))
            self._write_bytes(encode_hid_report(MOUSE_ZERO_RELEASE))
            self._write_bytes(encode_hid_report(abs_zero))
            holder["result"] = FenceResult(
                ok=True,
                neutral_reports=(KEYBOARD_RELEASE, MOUSE_ZERO_RELEASE, abs_zero),
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

    def _process_mouse_mode(self, payload) -> None:
        _, data, future = payload
        try:
            self._write_bytes(data)
        except TimeoutError:
            self._set_status("fault")
            self._complete(future, WriteOutcome(0, "TIMEOUT"))
            return
        except TransportClosed:
            self._set_status("disconnected")
            self._complete(future, WriteOutcome(0, "CLOSED"))
            return
        except Exception:
            self._set_status("fault")
            self._complete(future, WriteOutcome(0, "FAILED"))
            return
        # Refresh the persisted mode via the read-only query; the reader
        # thread updates device_info and notifies the broker when it lands.
        self._enqueue_control(("raw", build_device_info_query(), lambda outcome: None))
        self._complete(future, WriteOutcome(len(data), None))

    def _process_device_restart(self, payload) -> None:
        _, data, future = payload
        try:
            self._write_bytes(data)
            self._complete(future, WriteOutcome(len(data), None))
        except TimeoutError:
            # The device may already be rebooting; treat the write as sent.
            self._complete(future, WriteOutcome(len(data), None))
        except TransportClosed:
            self._set_status("disconnected")
            self._complete(future, WriteOutcome(0, "CLOSED"))
        except Exception:
            self._complete(future, WriteOutcome(0, "FAILED"))

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
                if not frame.checksum_ok:
                    continue
                if frame.kind == EXT_INFO_KIND:
                    self.device_info = parse_device_info(frame.payload)
                    raw = self.device_info.get("mouse_coordinate_mode_raw")
                    self._mouse_mode_raw = raw if isinstance(raw, int) else None
                    if self._on_device_info is not None:
                        callback, info = self._on_device_info, dict(self.device_info)
                        try:
                            self._loop.call_soon_threadsafe(callback, info)
                        except RuntimeError:  # loop closed during shutdown
                            pass
                elif frame.kind == EXT_GENERAL_REPLY_KIND and frame.channel == EXT_QUERY_CHANNEL:
                    reply = parse_general_reply(frame.payload)
                    pending = self._pending_ext_reply
                    if pending is not None and not pending["event"].is_set():
                        pending.update(reply)
                        pending["event"].set()
                    else:
                        logger.debug(
                            "unsolicited general reply rsp_code=%s app_id=%s",
                            reply["rsp_code"],
                            reply["app_id"],
                        )
                else:
                    # Known-unmanaged frames (e.g. 0x8E event reports, channels
                    # 2/6) are dropped by design; keep a bounded debug trace.
                    logger.debug(
                        "dropped ext frame channel=%s kind=%s size=%s",
                        frame.channel,
                        frame.kind,
                        frame.size,
                    )


class ShortWrite(Exception):
    pass
