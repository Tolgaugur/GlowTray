"""f.lux sync: read the color temperature f.lux applies to the screen from the gamma ramp and mirror it on the light."""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

import psutil

import light as L

log = logging.getLogger("glowtray.flux")
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
gdi32.CreateDCW.restype = wt.HDC
gdi32.CreateDCW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p]
gdi32.GetDeviceGammaRamp.argtypes = [wt.HDC, ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [wt.HDC]

K_MIN, K_MAX = 1200, 6500
BULB_CT_MIN = 2200  # anything warmer is sent as a color (xy)


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32), ("DeviceString", wt.WCHAR * 128),
                ("StateFlags", wt.DWORD), ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


def _primary_device():
    i = 0
    first = None
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        i += 1
        if d.StateFlags & 1:  # attached to desktop
            first = first or d.DeviceName
            if d.StateFlags & 4:  # primary display
                return d.DeviceName
    return first


def read_gamma():
    """Peak values (0..1) of the primary display's gamma ramp, or None."""
    name = _primary_device()
    if not name:
        return None
    dc = gdi32.CreateDCW("DISPLAY", name, None, None)
    if not dc:
        return None
    try:
        ramp = (ctypes.c_ushort * 768)()
        if not gdi32.GetDeviceGammaRamp(dc, ramp):
            return None
        return ramp[255] / 65535, ramp[511] / 65535, ramp[767] / 65535
    finally:
        gdi32.DeleteDC(dc)


def _norm(rgb):
    m = max(rgb) or 1
    return [c / m for c in rgb]


_TABLE = [(k, _norm(L.kelvin_to_rgb(k))) for k in range(K_MIN, K_MAX + 1, 50)]


def estimate(gamma):
    """Estimate (kelvin, screen_dim 0..1) from the gamma peak values."""
    r, g, b = gamma
    dim = max(r, g, b)
    if dim <= 0:
        return None, 0
    n = _norm((r, g, b))
    if min(n) > 0.985:
        return K_MAX, dim
    k = min(_TABLE, key=lambda kv: (kv[1][1] - n[1]) ** 2 + 1.5 * (kv[1][2] - n[2]) ** 2)[0]
    return k, dim


def flux_running():
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "flux.exe":
            return True
    return False


def _changed(a, b):
    """Are two (kelvin, brightness) readings different enough to be worth updating the light?"""
    if a is None or b is None:
        return a != b
    if abs((a[0] or 0) - (b[0] or 0)) >= FluxSync.K_STEP:
        return True
    if (a[1] is None) != (b[1] is None):
        return True
    return a[1] is not None and abs(a[1] - b[1]) >= FluxSync.B_STEP


class FluxSync(threading.Thread):
    POLL = 2.0       # s — f.lux polling interval
    MIN_GAP = 1.5    # s — minimum time between two light updates
    K_STEP = 75      # update after this many kelvin of change
    B_STEP = 3       # update after this much brightness (%) change

    def __init__(self, app):
        super().__init__(name="flux", daemon=True)
        self.app = app
        self.paused = False
        self.status = {}
        self._last_apply = 0.0
        self._applied = None    # last (kelvin, brightness) written to the light
        self._reading = None    # last f.lux reading (kelvin, brightness)
        self._pause_ref = None  # f.lux reading at the moment sync was paused
        self._was_on = None
        self._running = (0.0, False)
        self._wake = threading.Event()

    def _cfg(self):
        return self.app.cfg.get().get("flux", {})

    def target_bri(self, kelvin, dim):
        c = self._cfg()
        if not c.get("dim", True):
            return None
        lo, hi = float(c.get("min_bri", 30)), float(c.get("max_bri", 100))
        t = max(0.0, min(1.0, (kelvin - 1900) / (K_MAX - 1900)))
        return max(1, round((lo + (hi - lo) * t) * dim))

    def manual(self, kind):
        """The user changed the light manually: pause sync until f.lux changes again."""
        c = self._cfg()
        if not c.get("enabled") or self.paused:
            return
        if kind == "bri" and not c.get("dim", True):
            return
        self.paused = True
        self._pause_ref = self._reading
        log.info("f.lux sync paused due to manual change (%s)", kind)
        self._publish()

    def resume(self):
        self.paused = False
        self._pause_ref = None
        self.poke()

    def poke(self):
        self._applied = None
        self._last_apply = 0.0
        self._wake.set()

    def _publish(self, **kw):
        st = dict(self.status, **kw, paused=self.paused, enabled=bool(self._cfg().get("enabled")))
        if st != self.status:
            self.status = st
            self.app.push_flux(st)

    def _flux_running(self):
        ts, val = self._running
        if time.monotonic() - ts > 10:
            val = flux_running()
            self._running = (time.monotonic(), val)
        return val

    def run(self):
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("flux")
            # poll less often when sync is off and the panel is hidden (status is only shown in the panel)
            busy = self._cfg().get("enabled") or self.app.visible
            self._wake.wait(self.POLL if busy else 10)
            self._wake.clear()

    def _tick(self):
        c = self._cfg()
        running = self._flux_running()
        g = read_gamma() if running else None
        kelvin, dim = estimate(g) if g else (None, 1.0)
        tb = self.target_bri(kelvin, dim) if kelvin else None
        self._reading = (kelvin, tb) if kelvin else None
        self._publish(running=running, kelvin=kelvin, dim=round(dim, 3), target_bri=tb)

        st = self.app.light.snapshot()
        on = st.get("on")
        # turning the light back on ends the pause and applies the f.lux value right away
        if on and self._was_on is False:
            if self.paused:
                self.paused = False
                self._publish()
            self._applied = None
            self._last_apply = 0.0
        self._was_on = on
        if not c.get("enabled") or not kelvin:
            return
        # if f.lux changes after a manual change, follow f.lux again
        if self.paused and _changed(self._pause_ref, self._reading):
            self.paused = False
            self._applied = None
            log.info("f.lux changed, resuming sync")
            self._publish()
        if self.paused or not on or st.get("effect") or st.get("fading"):
            return
        if self._applied is not None and not _changed(self._applied, self._reading):
            return
        if time.monotonic() - self._last_apply < self.MIN_GAP:
            return
        if kelvin < BULB_CT_MIN and st.get("has_color", True):
            self.app.light.set_values(rgb=L.rgb_to_hex(L.kelvin_to_rgb(kelvin)), bri=tb)
        else:
            self.app.light.set_values(ct=max(BULB_CT_MIN, kelvin), bri=tb)
        self._applied = self._reading
        self._last_apply = time.monotonic()
        log.info("f.lux sync: %sK, brightness %s", kelvin, tb)
