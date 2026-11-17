"""A click that lands on a disabled window must not report a plain success.

Windows drops mouse input aimed at a window disabled by a modal dialog, so
Click answered "Single left clicked at (124,72)." while nothing happened and
the agent went on from a wrong state (#557).
"""

import asyncio
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop
from windows_mcp.tools.input import register

SINGLE_MONITOR = (0, 0, 1920, 1080)
MAIN_WINDOW = 0x1234
DIALOG_WINDOW = 0x5678


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> Desktop:
    """A Desktop that clicks for real but never touches this machine's windows."""
    instance = Desktop.__new__(Desktop)
    instance.desktop_state = None
    owner = MagicMock()
    owner.checkpoint_current.return_value = None
    owner.record_step_current.return_value = None
    monkeypatch.setattr(service, "get_controller", lambda: owner)
    monkeypatch.setattr(service.uia, "GetVirtualScreenRect", lambda: SINGLE_MONITOR)
    monkeypatch.setattr(service.uia, "Click", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.control_overlay, "ripple", lambda x, y: None)
    return instance


def _window_at(monkeypatch: pytest.MonkeyPatch, enabled: bool, title: str = "") -> None:
    """Answer the Win32 queries as if the point sat on one window of a dialog owner."""
    monkeypatch.setattr(Desktop, "_top_level_window_at", classmethod(lambda cls, x, y: MAIN_WINDOW))
    monkeypatch.setattr(service.win32gui, "IsWindowEnabled", lambda handle: enabled)
    popup = DIALOG_WINDOW if title else 0
    monkeypatch.setattr(service.win32gui, "GetWindow", lambda handle, index: popup)
    monkeypatch.setattr(
        service.win32gui, "GetWindowText", lambda handle: title if handle == DIALOG_WINDOW else ""
    )


def test_a_click_on_a_live_window_adds_no_warning(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    _window_at(monkeypatch, enabled=True)

    assert desktop.click(loc=[100, 200]) == ""


def test_a_click_on_a_disabled_window_names_the_dialog(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    _window_at(monkeypatch, enabled=False, title="Sign in")

    warning = desktop.click(loc=[100, 200])

    assert "disabled" in warning
    assert "Sign in" in warning


def test_a_click_on_a_disabled_window_without_a_popup_still_warns(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    _window_at(monkeypatch, enabled=False)

    warning = desktop.click(loc=[100, 200])

    assert "modal dialog" in warning
    assert "''" not in warning


def test_a_failing_window_lookup_does_not_break_the_click(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The click already happened; a Win32 hiccup while explaining it must not surface."""

    def boom(*args: object) -> None:
        raise OSError("window is gone")

    monkeypatch.setattr(Desktop, "_top_level_window_at", classmethod(lambda cls, x, y: MAIN_WINDOW))
    monkeypatch.setattr(service.win32gui, "IsWindowEnabled", boom)

    assert desktop.click(loc=[100, 200]) == ""


def test_hover_only_reports_no_warning(desktop: Desktop, monkeypatch: pytest.MonkeyPatch) -> None:
    """clicks=0 only moves the pointer, so nothing was dropped."""
    _window_at(monkeypatch, enabled=False, title="Sign in")

    assert desktop.click(loc=[100, 200], clicks=0) == ""


def test_the_click_tool_repeats_the_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = MagicMock()
    desktop.click.return_value = "Warning: the window under the point is disabled."
    mcp = FakeMCP()
    register(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)

    answer = asyncio.run(mcp.tools["Click"](loc=[10, 20]))

    assert answer == (
        "Single left clicked at (10,20). Warning: the window under the point is disabled."
    )


def test_type_behind_a_modal_dialog_types_nothing(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The click on the field is dropped, so the keys would go to the dialog's own control."""
    _window_at(monkeypatch, enabled=False, title="Change payment")
    keys: list[str] = []
    monkeypatch.setattr(service.uia, "SendKeys", lambda key, *args, **kwargs: keys.append(key))
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(service.uia, "Click", lambda x, y, *args, **kwargs: clicks.append((x, y)))

    with pytest.raises(RuntimeError, match="Did not type.*'Change payment'"):
        desktop.type(loc=(100, 200), text="-99,39", clear=True, press_enter=True)

    assert keys == []
    assert clicks == []


def test_type_on_a_live_window_still_types(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    _window_at(monkeypatch, enabled=True)
    keys: list[str] = []
    monkeypatch.setattr(service.uia, "SendKeys", lambda key, *args, **kwargs: keys.append(key))
    monkeypatch.setattr(service.uia, "GetFocusedControl", lambda: None)

    desktop.type(loc=(100, 200), text="42")

    assert keys == ["4", "2"]


class FakeWindow:
    def __init__(
        self,
        rect: tuple[int, int, int, int],
        *,
        visible: bool = True,
        click_through: bool = False,
        pid: int = 4242,
        cloaked: bool = False,
        class_name: str = "#32770",
    ) -> None:
        self.class_name = class_name
        self.rect = rect
        self.visible = visible
        self.click_through = click_through
        self.pid = pid
        self.cloaked = cloaked


def _windows_in_z_order(monkeypatch: pytest.MonkeyPatch, windows: dict[int, FakeWindow]) -> None:
    """EnumWindows lists the handles front to back, as Windows does."""

    def enum_windows(callback, extra) -> None:
        for handle in windows:
            if callback(handle, extra) is False:
                raise RuntimeError("EnumWindows stopped by its callback")

    monkeypatch.setattr(service.win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(service.win32gui, "IsWindowVisible", lambda h: windows[h].visible)
    monkeypatch.setattr(
        service.win32gui,
        "GetWindowLong",
        lambda h, index: service.win32con.WS_EX_TRANSPARENT if windows[h].click_through else 0,
    )
    monkeypatch.setattr(
        service.win32process, "GetWindowThreadProcessId", lambda h: (1, windows[h].pid)
    )
    monkeypatch.setattr(Desktop, "_is_cloaked", staticmethod(lambda h: windows[h].cloaked))
    monkeypatch.setattr(service.win32gui, "GetWindowRect", lambda h: windows[h].rect)
    monkeypatch.setattr(service.win32gui, "GetClassName", lambda h: windows[h].class_name)


def test_the_hit_test_finds_a_disabled_owner_that_window_from_point_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WindowFromPoint skips disabled windows, so it never named the owner a modal disables."""
    dialog, owner, behind = 0x10, 0x20, 0x30
    _windows_in_z_order(
        monkeypatch,
        {
            dialog: FakeWindow((400, 300, 800, 600)),
            owner: FakeWindow((0, 0, 1280, 800)),
            behind: FakeWindow((0, 0, 1920, 1080)),
        },
    )

    assert Desktop._top_level_window_at(100, 100) == owner
    assert Desktop._top_level_window_at(500, 400) == dialog


def test_the_hit_test_skips_windows_nobody_can_click(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay, hidden, cloaked, click_through, tooltip, app = 0x10, 0x20, 0x30, 0x40, 0x45, 0x50
    _windows_in_z_order(
        monkeypatch,
        {
            # A tooltip over a disabled owner must not hide the modal that disables it.
            tooltip: FakeWindow((0, 0, 1920, 1080), class_name="tooltips_class32"),
            overlay: FakeWindow((0, 0, 1920, 1080), pid=service.os.getpid()),
            hidden: FakeWindow((0, 0, 1920, 1080), visible=False),
            cloaked: FakeWindow((0, 0, 1920, 1080), cloaked=True),
            click_through: FakeWindow((0, 0, 1920, 1080), click_through=True),
            app: FakeWindow((0, 0, 1280, 800)),
        },
    )

    assert Desktop._top_level_window_at(100, 100) == app
    assert Desktop._top_level_window_at(1500, 900) == 0
