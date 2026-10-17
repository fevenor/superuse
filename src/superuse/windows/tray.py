"""Notification-area tray (pystray) driving the broker on a background thread.

The tray loop owns the main thread (Windows requirement); the broker's
asyncio core runs on a daemon thread. GUI callbacks never touch control
state directly — they submit work onto the broker loop
(``loop.call_soon_threadsafe``) and open the default browser off-thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import webbrowser
from collections.abc import Callable

from superuse.core.models import Mode

logger = logging.getLogger(__name__)

STATE_LABELS = {
    Mode.IDLE: "待连接/空闲",
    Mode.AGENT: "agent 控制中",
    Mode.HUMAN: "人工控制",
    Mode.HUMAN_HOLD: "人工锁定",
    Mode.TRANSITION: "接管屏障中",
    Mode.RECOVERY: "恢复中",
    Mode.STOPPING: "停止中",
}


def state_label(mode: str | Mode, simulated: bool) -> str:
    key = mode if isinstance(mode, Mode) else Mode(mode)
    label = STATE_LABELS.get(key, str(mode))
    return f"{label}（模拟）" if simulated else label


class TrayApp:
    """Menu construction and action plumbing; pystray is loaded lazily."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        on_open_ui: Callable[[], None],
        on_takeover_and_open: Callable[[], None],
        on_handback: Callable[[], None],
        on_copy_diagnostics: Callable[[], str],
        on_quit: Callable[[], None],
        get_state_summary: Callable[[], str],
        icon_path: str | None = None,
    ) -> None:
        self._loop = loop
        self._on_open_ui = on_open_ui
        self._on_takeover_and_open = on_takeover_and_open
        self._on_handback = on_handback
        self._on_copy_diagnostics = on_copy_diagnostics
        self._on_quit = on_quit
        self._get_state_summary = get_state_summary
        self._icon_path = icon_path
        self._icon = None

    # -- thread-safe dispatch ---------------------------------------------

    def _dispatch(self, fn: Callable, *args) -> None:
        """Run a (possibly async) handler as a task on the broker loop.

        Handlers must never block the loop: async handlers are scheduled, and
        sync handlers must stay cheap. Waiting on a coroutine from the loop
        thread itself (.result()) would deadlock video/renewal processing
        (review R07).
        """

        def _run() -> None:
            try:
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                logger.exception("tray action failed")

        self._loop.call_soon_threadsafe(_run)

    def open_ui_clicked(self, _, __) -> None:
        self._dispatch(self._on_open_ui)

    def takeover_clicked(self, _, __) -> None:
        self._dispatch(self._on_takeover_and_open)

    def handback_clicked(self, _, __) -> None:
        self._dispatch(self._on_handback)

    def copy_diagnostics_clicked(self, _, __) -> None:
        text = self._on_copy_diagnostics()  # read-only snapshot; cheap
        self._dispatch(_copy_to_clipboard, text)

    def quit_clicked(self, _, __) -> None:
        self._dispatch(self._on_quit)

    # -- menu --------------------------------------------------------------

    def build_menu(self):
        from pystray import Menu, MenuItem

        return Menu(
            MenuItem("打开界面", self.open_ui_clicked, default=True),
            MenuItem(lambda text: self._get_state_summary(), None, enabled=False),
            MenuItem("接管并打开界面", self.takeover_clicked),
            MenuItem("交还给 agent", self.handback_clicked),
            MenuItem("复制脱敏诊断", self.copy_diagnostics_clicked),
            Menu.SEPARATOR,
            MenuItem("退出", self.quit_clicked),
        )

    # -- main-thread loop ---------------------------------------------------

    def run(self) -> None:
        """Block the main thread in the tray message loop until quit."""
        import pystray
        from PIL import Image

        image = (
            Image.open(self._icon_path)
            if self._icon_path
            else Image.new("RGB", (64, 64), (22, 33, 62))
        )
        self._icon = pystray.Icon(
            "superuse",
            icon=image,
            title="Superuse",
            menu=self.build_menu(),
        )
        self._icon.run()

    def stop(self) -> None:
        """End the tray loop; callable from any thread (the quit handler)."""
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # pragma: no cover - backend edge cases
                logger.exception("tray stop failed")


def _copy_to_clipboard(text: str) -> None:
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:  # pragma: no cover - clipboard may be locked
        logger.warning("clipboard copy failed")


def open_browser(url: str) -> None:
    # Browsers open off the tray thread; failures keep the app running and
    # are surfaced via diagnostics (docs/development/windows-operations.md §1).
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
