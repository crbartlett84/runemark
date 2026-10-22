"""Windows Explorer integration: the Tags field, thumbnail refresh, Recycle Bin, right-click menu.

On Windows, Explorer's Tags (System.Keywords) are read and written through the Windows
property system, the same code Explorer itself uses. If that fails, or on other systems,
Runemark writes the same data directly (see media/xtra.py).
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

from .media import xtra
from .paths import IS_WINDOWS

log = logging.getLogger(__name__)

EXPLORER_TAG_EXTENSIONS = {".mp4", ".m4v"}
MODE_AUTO = "auto"
MODE_BUILTIN = "builtin"


def supported(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in EXPLORER_TAG_EXTENSIONS


def windows_api_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import pythoncom  # noqa: F401
        from win32com.propsys import propsys, pscon  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def _com():
    """COM must be initialised on whichever thread talks to the property system."""
    import pythoncom
    initialised = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialised = True
    except pythoncom.com_error:
        pass  # already initialised on this thread (possibly in another mode): fine to use
    try:
        yield
    finally:
        if initialised:
            pythoncom.CoUninitialize()


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(";") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
GPS_DEFAULT = 0
GPS_READWRITE = 2


def _iid():
    import pythoncom
    from win32com.propsys import propsys
    return getattr(propsys, "IID_IPropertyStore", None) or pythoncom.MakeIID(IID_IPROPERTYSTORE)


def read_windows(path: str | os.PathLike) -> list[str]:
    """Explorer's Tags as Windows reports them."""
    from win32com.propsys import propsys, pscon
    with _com():
        store = propsys.SHGetPropertyStoreFromParsingName(str(Path(path).resolve()), None, GPS_DEFAULT, _iid())
        try:
            return _as_list(store.GetValue(pscon.PKEY_Keywords).GetValue())
        finally:
            del store


def _key(name: str):
    from win32com.propsys import propsys
    return propsys.PSGetPropertyKeyFromName(name)


def read_windows_value(path: str | os.PathLike, name: str):
    """Any property as Windows reports it, e.g. 'System.Rating' or 'System.Media.Year'."""
    from win32com.propsys import propsys
    with _com():
        store = propsys.SHGetPropertyStoreFromParsingName(str(Path(path).resolve()), None, GPS_DEFAULT, _iid())
        try:
            return store.GetValue(_key(name)).GetValue()
        finally:
            del store


def write_windows(path: str | os.PathLike, tags: list[str], rating: int | None = None) -> None:
    """Set Explorer's Tags (and the 1-99 rating, unless None) in one commit."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    with _com():
        store = propsys.SHGetPropertyStoreFromParsingName(str(Path(path).resolve()), None, GPS_READWRITE, _iid())
        try:
            if tags:
                value = propsys.PROPVARIANTType(list(tags), pythoncom.VT_VECTOR | pythoncom.VT_LPWSTR)
            else:
                value = propsys.PROPVARIANTType(None, pythoncom.VT_EMPTY)
            store.SetValue(pscon.PKEY_Keywords, value)
            if rating is not None:
                stars = propsys.PROPVARIANTType(int(rating), pythoncom.VT_UI4) if rating \
                    else propsys.PROPVARIANTType(None, pythoncom.VT_EMPTY)
                store.SetValue(_key("System.Rating"), stars)
            store.Commit()
        finally:
            del store


def read_tags(path: str | os.PathLike) -> list[str]:
    """Explorer's Tags for an MP4. Uses Windows when available, the file itself otherwise."""
    if windows_api_available():
        try:
            return read_windows(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Windows property read failed for %s: %s; reading the file directly", path, exc)
    return xtra.read_category(path)


def read_rating(path: str | os.PathLike) -> int:
    """Explorer's rating (0 = none, else 1-99). Uses Windows when available."""
    if windows_api_available():
        try:
            value = read_windows_value(path, "System.Rating")
            return int(value or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("Windows rating read failed for %s: %s; reading the file directly", path, exc)
    return xtra.read_rating(path)


def write_tags(path: str | os.PathLike, tags: list[str], mode: str = MODE_AUTO, rating: int | None = None) -> str:
    """Write Explorer's Tags, and the rating (1-99, 0 clears) unless None.
    Returns which method succeeded: 'windows' or 'builtin'.

    Raises xtra.NeedsPadding when the built-in writer has no room (the caller makes room
    by re-saving the MP4 tags with more padding, then tries again).
    """
    if mode == MODE_AUTO and windows_api_available():
        try:
            write_windows(path, tags, rating)
            return "windows"
        except Exception as exc:  # noqa: BLE001
            log.warning("Windows property write failed for %s: %s; using the built-in writer", path, exc)
    updates: dict[str, list] = {xtra.CATEGORY: list(tags)}
    if rating is not None:
        updates[xtra.RATING] = [int(rating)] if rating else []
    xtra.write_entries(path, updates)
    return "builtin"


def notify_changed(path: str | os.PathLike) -> None:
    """Ask Explorer to refresh the file's details and thumbnail."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        SHCNE_UPDATEITEM = 0x00002000
        SHCNF_PATHW = 0x0005
        ctypes.windll.shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW,
                                             ctypes.c_wchar_p(str(Path(path).resolve())), None)
    except Exception as exc:  # noqa: BLE001
        log.info("SHChangeNotify failed: %s", exc)


def recycle(path: str | os.PathLike) -> None:
    """Send a file to the Recycle Bin (the desktop trash on other systems)."""
    from send2trash import send2trash
    send2trash(str(path))


def set_dark_title_bar(window_id: int, dark: bool) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, then older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(window_id), attribute,
                                                          ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception as exc:  # noqa: BLE001
        log.info("dark title bar not applied: %s", exc)


# --------------------------------------------------------------------------
# "Tag with Runemark" in Explorer's right-click menu (current user only)
# --------------------------------------------------------------------------

MENU_KEY = r"Software\Classes\SystemFileAssociations\{ext}\shell\Runemark"
MENU_EXTENSIONS = (".mp4", ".m4v", ".mkv")


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" -m runemark "%1"'


def context_menu_installed() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MENU_KEY.format(ext=".mp4")):
            return True
    except OSError:
        return False


def install_context_menu() -> None:
    import winreg
    command = launch_command()
    icon = sys.executable if getattr(sys, "frozen", False) else ""
    for ext in MENU_EXTENSIONS:
        key_path = MENU_KEY.format(ext=ext)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "Tag with Runemark")
            if icon:
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)


def remove_context_menu() -> None:
    import winreg
    for ext in MENU_EXTENSIONS:
        key_path = MENU_KEY.format(ext=ext)
        for sub in (key_path + r"\command", key_path):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass
