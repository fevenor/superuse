"""Notification-area tray (pystray) driving the broker on a background thread.

The tray loop owns the main thread (Windows requirement); the broker's
asyncio core runs on a daemon thread. GUI callbacks never touch control
state directly — they submit work onto the broker loop
(``loop.call_soon_threadsafe``) and open the default browser off-thread.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import webbrowser
from collections.abc import Callable

from superuse.core.models import Mode

logger = logging.getLogger(__name__)


def _create_icon(*args, **kwargs):
    """Windows 菜单显示前读取最新状态，避免异步操作后的旧勾选状态。"""
    import pystray

    if sys.platform != "win32":
        return pystray.Icon(*args, **kwargs)

    from pystray._util import win32

    class CurrentMenuIcon(pystray.Icon):
        def _on_notify(self, wparam, lparam):
            # pystray 0.19.5 只在点击回调返回时刷新，后台任务此时可能尚未完成。
            # 在托盘线程弹出菜单前重建，网页修改和异步失败也读取实际状态。
            if lparam == win32.WM_RBUTTONUP:
                self.update_menu()
            return super()._on_notify(wparam, lparam)

    return CurrentMenuIcon(*args, **kwargs)


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
        on_rescan_devices: Callable[[], None] | None = None,
        on_set_lan: Callable[[bool], object] | None = None,
        lan_enabled: Callable[[], bool] | None = None,
        lan_toggle_available: Callable[[], bool] | None = None,
        lan_address_text: Callable[[], str] | None = None,
        on_set_keep_awake: Callable[[bool], object] | None = None,
        keep_awake_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._loop = loop
        self._on_open_ui = on_open_ui
        self._on_takeover_and_open = on_takeover_and_open
        self._on_handback = on_handback
        self._on_copy_diagnostics = on_copy_diagnostics
        self._on_quit = on_quit
        self._get_state_summary = get_state_summary
        self._icon_path = icon_path
        self._on_rescan_devices = on_rescan_devices
        self._on_set_lan = on_set_lan
        self._lan_enabled = lan_enabled or (lambda: False)
        self._lan_toggle_available = lan_toggle_available or (lambda: False)
        self._lan_address_text = lan_address_text or (lambda: "")
        self._icon = None
        self._on_set_keep_awake = on_set_keep_awake
        self._keep_awake_enabled = keep_awake_enabled or (lambda: False)

    # -- thread-safe dispatch ---------------------------------------------

    def _dispatch(self, fn: Callable, *args) -> None:
        """在服务循环中执行回调，并统一记录同步及异步失败。

        异步回调使用 await，不能同步等待 Future.result()，以免阻塞视频和续租。
        同步回调仍须保持轻量。
        """

        async def _run() -> None:
            try:
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("托盘操作失败")

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_run()))

    def open_ui_clicked(self, _, __) -> None:
        self._dispatch(self._on_open_ui)

    def takeover_clicked(self, _, __) -> None:
        self._dispatch(self._on_takeover_and_open)

    def handback_clicked(self, _, __) -> None:
        self._dispatch(self._on_handback)

    def rescan_clicked(self, _, __) -> None:
        if self._on_rescan_devices is not None:
            self._dispatch(self._on_rescan_devices)

    def lan_toggled(self, item) -> None:
        if self._on_set_lan is None:
            return
        # The checkbox state is read from the broker, not from the menu item:
        # a failed transaction must not leave a checked-but-off menu.
        target = not self._lan_enabled()
        self._dispatch(self._on_set_lan, target)

    def copy_lan_addresses_clicked(self, _, __) -> None:
        text = self._lan_address_text()
        self._dispatch(_copy_to_clipboard, text)

    def keep_awake_toggled(self, item) -> None:
        if self._on_set_keep_awake is not None:
            self._dispatch(self._on_set_keep_awake, not self._keep_awake_enabled())

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
            MenuItem("重新检测设备", self.rescan_clicked),
            Menu.SEPARATOR,
            MenuItem(
                "允许局域网访问",
                self.lan_toggled,
                checked=lambda item: self._lan_enabled(),
                enabled=lambda item: self._lan_toggle_available(),
            ),
            MenuItem("复制局域网地址", self.copy_lan_addresses_clicked),
            MenuItem(
                "保持本机唤醒（允许熄屏）",
                self.keep_awake_toggled,
                checked=lambda item: self._keep_awake_enabled(),
            ),
            Menu.SEPARATOR,
            MenuItem("接管并打开界面", self.takeover_clicked),
            MenuItem("交还给 agent", self.handback_clicked),
            MenuItem("复制脱敏诊断", self.copy_diagnostics_clicked),
            Menu.SEPARATOR,
            MenuItem("退出", self.quit_clicked),
        )

    # -- main-thread loop ---------------------------------------------------

    def run(self) -> None:
        """Block the main thread in the tray message loop until quit."""
        from PIL import Image

        image = (
            Image.open(self._icon_path)
            if self._icon_path
            else Image.new("RGB", (64, 64), (22, 33, 62))
        )
        self._icon = _create_icon(
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
