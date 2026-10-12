"""Screen color sync: extract the dominant color and brightness from a downscaled capture of the chosen display."""
import colorsys
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")

user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GdiFlush.restype = wt.BOOL

W, H = 64, 36
HALFTONE, SRCCOPY = 4, 0x00CC0020
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def list_monitors():
    """[{'id': '\\\\.\\DISPLAY1', 'label': 'Display 1 · 2560×1440 (primary)', 'rect': (l, t, r, b), 'primary': bool}]"""
    out = []

    def cb(hmon, hdc, rect, _):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        out.append({"id": mi.szDevice, "rect": (r.left, r.top, r.right, r.bottom), "primary": bool(mi.dwFlags & 1)})
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    out.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    for i, m in enumerate(out, 1):
        l, t, r, b = m["rect"]
        m["label"] = f"Display {i} · {r - l}×{b - t}" + (" (primary)" if m["primary"] else "")
    return out


class ScreenSync:
    """Samples the screen on every call; returns a smoothed {'rgb', 'bri'} or {'ct', 'bri'}."""

    def __init__(self, get_settings):
        self._settings = get_settings
        self._lock = threading.Lock()
        self._rgb = None
        self._bri = None
        self._sent = (None, None)

    def _capture(self, rect):
        l, t, r, b = rect
        sdc = user32.GetDC(None)
        mdc = gdi32.CreateCompatibleDC(sdc)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mdc, hbm)
        try:
            gdi32.SetStretchBltMode(mdc, HALFTONE)
            gdi32.SetBrushOrgEx(mdc, 0, 0, None)
            gdi32.StretchBlt(mdc, 0, 0, W, H, sdc, l, t, r - l, b - t, SRCCOPY)
            gdi32.GdiFlush()
            return ctypes.string_at(bits, W * H * 4)  # BGRA
        finally:
            gdi32.SelectObject(mdc, old)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(None, sdc)

    @staticmethod
    def analyze(data):
        """Find the dominant color (weighting saturated, bright pixels) and the average brightness."""
        bins = [0.0] * 24
        acc = [[0.0, 0.0, 0.0] for _ in range(24)]
        lum = 0.0
        n = len(data) // 4
        for i in range(0, len(data), 4):
            b, g, r = data[i], data[i + 1], data[i + 2]
            lum += 0.2126 * r + 0.7152 * g + 0.0722 * b
            mx = max(r, g, b)
            if mx < 24:
                continue
            mn = min(r, g, b)
            s = (mx - mn) / mx
            w = s * s * (mx / 255)
            if w < 0.02:
                continue
            h = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0]
            k = int(h * 24) % 24
            bins[k] += w
            a = acc[k]
            a[0] += r * w
            a[1] += g * w
            a[2] += b * w
        lum = lum / max(1, n) / 255
        total = sum(bins)
        if total / max(1, n) < 0.025:
            return None, lum  # mostly grey/black/white
        k = max(range(24), key=lambda i: bins[i - 1] + bins[i] * 1.5 + bins[(i + 1) % 24])
        idx = [(k - 1) % 24, k, (k + 1) % 24]
        wsum = sum(bins[i] for i in idx)
        r, g, b = (sum(acc[i][c] for i in idx) / wsum for c in range(3))
        h, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 1.15), 1.0)  # brightness is set separately
        return (r * 255, g * 255, b * 255), lum

    def sample(self):
        st = self._settings()
        mons = list_monitors()
        mon = next((m for m in mons if m["id"] == st.get("monitor")), None) or (mons[0] if mons else None)
        if not mon:
            return None
        color, lum = self.analyze(self._capture(mon["rect"]))
        if color is None:
            color = (255, 200, 150)  # colorless scene: warm white
        lo, hi = float(st.get("min_bri", 5)), float(st.get("max_bri", 100))
        bri = lo + (hi - lo) * min(1.0, lum * 1.8) if st.get("follow_bri", True) else hi
        with self._lock:
            if self._rgb is None:
                self._rgb, self._bri = list(color), bri
            else:
                a = 0.55
                self._rgb = [p + (c - p) * a for p, c in zip(self._rgb, color)]
                self._bri += (bri - self._bri) * 0.4
            rgb = tuple(round(v) for v in self._rgb)
            bri = round(self._bri)
            last_rgb, last_bri = self._sent
            out = {}
            if last_rgb is None or sum(abs(x - y) for x, y in zip(rgb, last_rgb)) > 18:
                out["rgb"] = "#%02x%02x%02x" % rgb
                last_rgb = rgb
            if last_bri is None or abs(bri - last_bri) >= 3:
                out["bri"] = max(1, bri)
                last_bri = bri
            self._sent = (last_rgb, last_bri)
            return out or None
