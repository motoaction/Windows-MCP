import sys
from types import SimpleNamespace

import pytest

if sys.platform == "win32":
    from windows_mcp.desktop import service
    from windows_mcp.desktop.service import Desktop
    from windows_mcp.powershell import PowerShellExecutor
else:
    pytestmark = pytest.mark.skip(reason="Desktop imports Windows-only pywin32 modules")


class _Folder:
    def __init__(self, items: dict[str, str]):
        self._items = items

    def Items(self):
        return [SimpleNamespace(Name=name, Path=path) for name, path in self._items.items()]

    def ParseName(self, app_id: str):
        return object() if app_id in self._items.values() else None


def _use_apps_folder(monkeypatch: pytest.MonkeyPatch, folder: "_Folder | None") -> None:
    shell_app = SimpleNamespace(NameSpace=lambda _: folder)
    monkeypatch.setattr(service.win32com.client, "Dispatch", lambda _: shell_app)


def _record_shell_execute(monkeypatch: pytest.MonkeyPatch, process_handle=None) -> list[dict]:
    calls: list[dict] = []

    def shell_execute_ex(**kwargs):
        calls.append(kwargs)
        return {"hInstApp": 42, "hProcess": process_handle}

    monkeypatch.setattr(service.shell, "ShellExecuteEx", shell_execute_ex)
    return calls


@pytest.fixture(autouse=True)
def no_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    def execute_command(command: str, *_: object) -> tuple[str, int]:
        pytest.fail(f"App launch started PowerShell: {command}")

    monkeypatch.setattr(PowerShellExecutor, "execute_command", staticmethod(execute_command))


def test_start_menu_apps_include_localized_appsfolder_name(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"
    _use_apps_folder(monkeypatch, _Folder({"记事本": app_id}))
    calls = _record_shell_execute(monkeypatch)

    assert Desktop.__new__(Desktop).launch_app("记事本") == ("", 0, 0)
    assert calls[-1]["lpFile"] == f"shell:AppsFolder\\{app_id}"


def test_start_menu_shortcut_fallback_is_preserved_when_appsfolder_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shortcut_apps = {
        "legacy app": r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Legacy.lnk"
    }
    _use_apps_folder(monkeypatch, None)
    desktop = Desktop.__new__(Desktop)
    monkeypatch.setattr(desktop, "_get_apps_from_shortcuts", lambda: shortcut_apps)

    assert desktop.get_apps_from_start_menu() == shortcut_apps


def test_launching_a_path_returns_the_started_process_id(monkeypatch: pytest.MonkeyPatch) -> None:
    exe_path = r"C:\Program Files\Legacy\legacy.exe"
    _use_apps_folder(monkeypatch, _Folder({"Legacy App": exe_path}))
    process_handle = SimpleNamespace(Close=lambda: None)
    calls = _record_shell_execute(monkeypatch, process_handle)
    monkeypatch.setattr(service.win32process, "GetProcessId", lambda handle: 4321)

    assert Desktop.__new__(Desktop).launch_app("Legacy App") == ("", 0, 4321)
    assert calls[-1]["lpFile"] == exe_path


def test_a_refused_launch_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    exe_path = r"C:\Program Files\Legacy\legacy.exe"
    _use_apps_folder(monkeypatch, _Folder({"Legacy App": exe_path}))

    def refused(**_):
        raise service.pywintypes.error(1260, "ShellExecuteEx", "This program is blocked.")

    monkeypatch.setattr(service.shell, "ShellExecuteEx", refused)

    response, status, pid = Desktop.__new__(Desktop).launch_app("Legacy App")
    assert (status, pid) == (1, 0)
    assert response == "Failed to launch Legacy App: This program is blocked."
