"""通过 Windows 原生菜单验证异步及网页操作后的勾选状态。"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 原生菜单")


async def test_native_menu_tracks_async_and_external_changes(monkeypatch, caplog):
    import pystray
    from PIL import Image
    from pystray._util import win32

    from superuse.windows.tray import TrayApp, _create_icon

    enabled = False
    fail = False
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()

    async def change(value):
        nonlocal enabled
        started.set()
        await finish.wait()
        try:
            if fail:
                raise OSError("测试：无法创建电源请求")
            enabled = value
        finally:
            completed.set()

    app = TrayApp(
        loop=asyncio.get_running_loop(),
        on_open_ui=lambda: None,
        on_takeover_and_open=lambda: None,
        on_handback=lambda: None,
        on_copy_diagnostics=lambda: "",
        on_quit=lambda: None,
        get_state_summary=lambda: "空闲",
        on_set_keep_awake=change,
        keep_awake_enabled=lambda: enabled,
    )
    icon = _create_icon("test", Image.new("RGB", (16, 16)), menu=app.build_menu())
    app._icon = icon
    get_state = ctypes.WinDLL("user32").GetMenuState
    get_state.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
    get_state.restype = wintypes.UINT
    index = next(i for i, item in enumerate(icon.menu.items) if "保持本机唤醒" in item.text)
    observed = []

    def before_popup(self, _wparam, _lparam):
        # 仅阻止真正弹出窗口；菜单生成、回调包装及勾选读取均使用原生实现。
        bits = get_state(self._menu_handle[0], index, 0x0400)
        assert bits != 0xFFFFFFFF
        observed.append(bool(bits & 0x0008))

    monkeypatch.setattr(pystray.Icon, "_on_notify", before_popup)
    try:
        icon._on_notify(0, win32.WM_RBUTTONUP)
        assert observed[-1] is False
        icon._menu_handle[1][index](icon)
        await started.wait()
        assert enabled is False
        finish.set()
        await completed.wait()
        assert enabled is True
        icon._on_notify(0, win32.WM_RBUTTONUP)
        assert observed[-1] is True

        # 模拟网页关闭；下一次菜单打开应直接反映实际状态。
        enabled = False
        icon._on_notify(0, win32.WM_RBUTTONUP)
        assert observed[-1] is False

        fail = True
        completed.clear()
        icon._menu_handle[1][index](icon)
        await completed.wait()
        icon._on_notify(0, win32.WM_RBUTTONUP)
        assert observed[-1] is False
        assert "托盘操作失败" in caplog.text
    finally:
        win32.DestroyMenu(icon._menu_handle[0])
