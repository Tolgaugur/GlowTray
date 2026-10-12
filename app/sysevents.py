"""System events via a hidden window: lock, sleep, shutdown and global hotkeys."""
import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger("glowtray.sys")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


WM_QUERYENDSESSION = 0x11
WM_POWERBROADCAST = 0x218
WM_WTSSESSION_CHANGE = 0x2B1
WM_HOTKEY = 0x312
WM_APP_REHOOK = 0x8001
WM_APP_SHOW = 0x8002
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000

HOTKEY_IDS = {"toggle": 1, "panel": 2, "bri_up": 3, "bri_down": 4}
_SPECIAL = {"SPACE": 0x20, "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27, "HOME": 0x24, "END": 0x23,
            "PAGEUP": 0x21, "PAGEDOWN": 0x22, "INSERT": 0x2D, "PAUSE": 0x13, "+": 0xBB, "-": 0xBD,
            ",": 0xBC, ".": 0xBE, "NUMPLUS": 0x6B, "NUMMINUS": 0x6D}


def parse_hotkey(text):
    """'Ctrl+Alt+L' -> (mods, vk) or None."""
    if not text:
        return None
    mods, vk = 0, None
    for part in [p.strip() for p in text.replace(" ", "").split("+") if p.strip()] or []:
        u = part.upper()
        if u in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif u == "ALT":
            mods |= MOD_ALT
        elif u == "SHIFT":
            mods |= MOD_SHIFT
        elif u in ("WIN", "META"):
            mods |= MOD_WIN
        elif len(u) == 1 and (u.isalpha() or u.isdigit()):
            vk = ord(u)
        elif u.startswith("F") and u[1:].isdigit() and 1 <= int(u[1:]) <= 24:
            vk = 0x6F + int(u[1:])
        elif u in _SPECIAL:
            vk = _SPECIAL[u]
    # cases like "Ctrl++"
    if vk is None and text.endswith("++"):
        vk = 0xBB
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


class SysEvents(threading.Thread):
    def __init__(self, on_event):
        super().__init__(name="sysevents", daemon=True)
        self._on_event = on_event
        self.hwnd = None
        self._hotkeys = {}
        self._wanted = {}
        self.hotkey_errors = []
        self._ready = threading.Event()
        self.locked = False

    def set_hotkeys(self, mapping):
        """mapping: {'toggle': 'Ctrl+Alt+L', ...}"""
        self._wanted = dict(mapping)
        self._ready.wait(5)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REHOOK, 0, 0)

    def _apply_hotkeys(self):
        for hid in list(self._hotkeys):
            user32.UnregisterHotKey(self.hwnd, hid)
        self._hotkeys.clear()
        errors = []
        for name, text in self._wanted.items():
            parsed = parse_hotkey(text)
            hid = HOTKEY_IDS.get(name)
            if not parsed or not hid:
                continue
            if user32.RegisterHotKey(self.hwnd, hid, parsed[0], parsed[1]):
                self._hotkeys[hid] = name
            else:
                errors.append(name)
                log.warning("could not register hotkey: %s (%s)", name, text)
        self.hotkey_errors = errors

    def _emit(self, event, **data):
        try:
            self._on_event(event, data)
        except Exception:
            log.exception("event %s", event)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == 7:
                    self.locked = True
                    self._emit("lock")
                elif wparam == 8:
                    self.locked = False
                    self._emit("unlock")
            elif msg == WM_POWERBROADCAST:
                if wparam == 4:
                    self._emit("sleep")
                elif wparam == 0x12:
                    self._emit("resume")
                return 1
            elif msg == WM_HOTKEY:
                name = self._hotkeys.get(wparam)
                if name:
                    self._emit("hotkey", name=name)
                return 0
            elif msg == WM_APP_SHOW:
                self._emit("show_panel")
                return 0
            elif msg == WM_APP_REHOOK:
                self._apply_hotkeys()
                return 0
            elif msg == WM_QUERYENDSESSION:
                self._emit("shutdown")
                return 1
        except Exception:
            log.exception("wndproc")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self._wndproc = WNDPROC(self._proc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "GlowtraySysEvents"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "GlowtraySysEvents", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        try:
            wtsapi32.WTSRegisterSessionNotification(self.hwnd, 0)
        except Exception:
            log.exception("WTS")
        self._apply_hotkeys()
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
