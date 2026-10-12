"""Win32 helpers: panel placement, focus, autostart, pinning the tray icon."""
import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
import winreg

log = logging.getLogger("glowtray.win")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.MonitorFromPoint.restype = wt.HANDLE
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.FindWindowW.restype = wt.HWND
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x4, 0x10, 0x20

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Glowtray"
LEGACY_RUN_NAME = "HueKontrol"  # autostart entry used before the rename


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def hwnd_of(window):
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return None


def make_tool_window(hwnd):
    """Hide from the taskbar and Alt+Tab."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def round_corners(hwnd):
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(pref), 4)


def dark_frame(hwnd, dark):
    v = ctypes.c_int(1 if dark else 0)
    dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), 20, ctypes.byref(v), 4)


def place_near_tray(hwnd, margin_logical=12):
    """Place the panel next to the taskbar on the taskbar's monitor."""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if tray:
        mon = user32.MonitorFromWindow(wt.HWND(tray), 2)
    else:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    work, full = mi.rcWork, mi.rcMonitor
    r = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    try:
        scale = user32.GetDpiForWindow(wt.HWND(hwnd)) / 96 or 1
    except Exception:
        scale = 1
    m = int(margin_logical * scale)
    x = work.right - w - m
    y = work.bottom - h - m
    if work.left > full.left:      # taskbar on the left
        x = work.left + m
    if work.top > full.top:        # taskbar at the top
        y = work.top + m
    user32.SetWindowPos(wt.HWND(hwnd), HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)


def force_foreground(hwnd):
    hwnd = wt.HWND(hwnd)
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_thread and fg_thread != cur:
        attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thread, False)


def foreground_pid():
    fg = user32.GetForegroundWindow()
    if not fg:
        return None, None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    root = user32.GetAncestor(fg, 3)  # GA_ROOTOWNER
    return pid.value, root


def idle_seconds():
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(li)):
        return 0
    return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000


def system_dark_mode():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def taskbar_light():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


# ---------- autostart ----------

def exe_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(sys.argv[0])}" --tray'


def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, RUN_NAME)[0])
    except OSError:
        return False


def set_autostart(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, LEGACY_RUN_NAME)
            except FileNotFoundError:
                pass
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, exe_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.exception("autostart")
        return False


def promote_tray_icon():
    """Windows 11: show the icon on the taskbar instead of in the hidden (^) overflow menu."""
    target = os.path.normcase(os.path.abspath(sys.executable))
    base = r"Control Panel\NotifyIconSettings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                        path = winreg.QueryValueEx(k, "ExecutablePath")[0]
                        if os.path.normcase(os.path.abspath(os.path.expandvars(path))) == target:
                            winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                            return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


# ---------- single instance ----------

_mutex = None


def single_instance(name="Local\\GlowtraySingleInstance"):
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def signal_existing():
    hwnd = user32.FindWindowW("GlowtraySysEvents", None)
    if hwnd:
        user32.PostMessageW(hwnd, 0x8002, 0, 0)
