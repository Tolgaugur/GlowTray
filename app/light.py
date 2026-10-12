"""Hue Bluetooth bulb controller.

Bluetooth work runs on its own asyncio loop in a separate thread. Other threads
call the synchronous methods here; writes are coalesced in a queue (when a slider
is dragged quickly, only the latest value is sent).
"""
import asyncio
import colorsys
import logging
import math
import random
import struct
import threading
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

log = logging.getLogger("glowtray.light")


def _u(n):
    return f"932c32bd-{n:04x}-47a2-835a-a8d455b859dd"


SERVICE = _u(0)
POWER, BRIGHTNESS, TEMPERATURE, COLOR = _u(2), _u(3), _u(4), _u(5)
NAME_CHAR = "97fe6561-0003-4f62-86e9-b71ee2da3d22"
PHILIPS_UUID = "0000fe0f-0000-1000-8000-00805f9b34fb"

MIRED_MIN, MIRED_MAX = 153, 454  # 6500K .. 2200K


# ---------- color conversions ----------

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def rgb_to_xy(r, g, b):
    def lin(c):
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = lin(r), lin(g), lin(b)
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    s = X + Y + Z
    return (0.3127, 0.3290) if s == 0 else (X / s, Y / s)


def xy_to_rgb(x, y):
    if y <= 0:
        return (255, 255, 255)
    Y = 1.0
    X = (Y / y) * x
    Z = (Y / y) * (1 - x - y)
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    m = max(r, g, b, 1e-9)
    out = []
    for c in (r / m, g / m, b / m):
        c = max(0.0, c)
        c = 12.92 * c if c <= 0.0031308 else (1.055 * c ** (1 / 2.4) - 0.055)
        out.append(max(0, min(255, round(c * 255))))
    return tuple(out)


def kelvin_to_rgb(k):
    """Approximate color, for display on screen."""
    t = k / 100
    r = 255 if t <= 66 else 329.698727446 * ((t - 60) ** -0.1332047592)
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * ((t - 60) ** -0.0755148492)
    b = 255 if t >= 66 else (0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
    return tuple(max(0, min(255, round(v))) for v in (r, g, b))


def friendly_error(e):
    s = f"{type(e).__name__}: {e}".lower()
    if "not found" in s or "was not found" in s or isinstance(e, asyncio.TimeoutError) or "timeout" in s:
        return "Bulb not found. A phone may be connected to it, or the bulb is powered off."
    if "unreachable" in s:
        return "Bulb unreachable. Close the Hue app on your phone and try again."
    if "access" in s or "denied" in s or "auth" in s:
        return "Access denied. You may need to pair the bulb with Windows again."
    return str(e) or type(e).__name__


class Light:
    def __init__(self, get_config, on_change):
        self._cfg = get_config
        self._on_change = on_change
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ble", daemon=True)
        self._client = None
        self._conn_lock = None
        self._pending = {}
        self._writer_task = None
        self._auto_task = None  # fade / effect
        # Rapidly changing values during an effect are not pushed to the UI (so sliders don't jitter);
        # the first value of each is shown, later ones wait here and are applied when the effect ends.
        self._fx_shown = set()
        self._fx_shadow = {}
        self._last_activity = time.monotonic()
        self._last_fail = 0.0
        self._holds = set()
        self._state_lock = threading.Lock()
        self.state = {
            "connected": False, "busy": False, "error": None,
            "on": None, "bri": None, "ct": None, "rgb": None, "mode": None,
            "has_color": True, "has_ct": True,
            "effect": None, "fading": False,
        }

    # ---------- plumbing ----------

    def start(self):
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._conn_lock = asyncio.Lock()
        self._loop.create_task(self._watchdog())
        self._loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _call_soon(self, fn, *args):
        self._loop.call_soon_threadsafe(fn, *args)

    def snapshot(self):
        with self._state_lock:
            return dict(self.state)

    def _set(self, **kw):
        changed = False
        with self._state_lock:
            for k, v in kw.items():
                if self.state.get(k) != v:
                    self.state[k] = v
                    changed = True
            snap = dict(self.state)
        if changed:
            try:
                self._on_change(snap)
            except Exception:
                log.exception("on_change")

    def _set_light(self, **kw):
        """Write light values to the state; while an effect runs, hold back values after the first one."""
        if self.state.get("effect"):
            for k in list(kw):
                if k == "on":
                    continue
                if k in self._fx_shown:
                    self._fx_shadow[k] = kw.pop(k)
                else:
                    self._fx_shown.add(k)
        if kw:
            self._set(**kw)

    def _fx_end(self):
        shadow = self._fx_shadow
        self._fx_shown, self._fx_shadow = set(), {}
        self._set(effect=None, fading=False, **shadow)

    def _touch(self):
        self._last_activity = time.monotonic()

    @property
    def address(self):
        return (self._cfg().get("device") or {}).get("address") or ""

    def hold(self, reason):
        """Keep the connection open (while the panel is open, an effect runs, ...)."""
        self._holds.add(reason)

    def release(self, reason):
        self._holds.discard(reason)
        self._touch()

    # ---------- connection ----------

    def _on_disconnect(self, client):
        # A late notification from an old/failed attempt must not drop the current connection
        if self._client is not None and client is not self._client:
            return
        log.info("connection lost")
        self._client = None
        self._set(connected=False)

    async def _ensure(self):
        if self._client is not None and self._client.is_connected:
            return True
        async with self._conn_lock:
            if self._client is not None and self._client.is_connected:
                return True
            addr = self.address
            if not addr:
                self._set(error="No bulb selected yet (Settings → Device).")
                return False
            self._set(busy=True, error=None)
            last_err = None
            for attempt in range(3):
                # Connect to the paired device directly without scanning (~2 s); fall back to scanning if that fails.
                target = BLEDevice(addr, self._cfg()["device"].get("name"), None) if attempt < 2 else addr
                client = BleakClient(target, services=[SERVICE], disconnected_callback=self._on_disconnect,
                                     timeout=10 if attempt < 2 else 15)
                try:
                    await client.connect()
                    await client.read_gatt_char(POWER)  # is the connection really up?
                    self._client = client
                    await self._after_connect()
                    self._last_fail = 0.0
                    self._touch()
                    log.info("connected %s", addr)
                    return True
                except Exception as e:
                    last_err = e
                    log.warning("connection attempt %d failed: %r", attempt + 1, e)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                    await asyncio.sleep(1)
            self._last_fail = time.monotonic()
            self._set(connected=False, busy=False, error=friendly_error(last_err))
            return False

    async def _after_connect(self):
        c = self._client
        chars = {ch.uuid.lower(): ch for s in c.services for ch in s.characteristics}
        has_color, has_ct = COLOR in chars, TEMPERATURE in chars
        st = await self._read_all(has_color, has_ct)
        self._set(connected=True, busy=False, error=None, has_color=has_color, has_ct=has_ct, **st)
        for u in (POWER, BRIGHTNESS, TEMPERATURE, COLOR):
            ch = chars.get(u)
            if ch is not None and "notify" in ch.properties:
                try:
                    await c.start_notify(ch, self._on_notify)
                except Exception as e:
                    log.debug("notify %s: %r", u, e)

    async def _read_all(self, has_color=True, has_ct=True):
        c = self._client
        st = {}
        st["on"] = bool((await c.read_gatt_char(POWER))[0])
        st["bri"] = round((await c.read_gatt_char(BRIGHTNESS))[0] / 254 * 100)
        if has_ct:
            try:
                mired = struct.unpack("<H", bytes(await c.read_gatt_char(TEMPERATURE))[:2])[0]
                if 100 <= mired <= 1000:
                    st["ct"] = round(1_000_000 / mired / 50) * 50
            except Exception:
                pass
        if has_color:
            try:
                raw = bytes(await c.read_gatt_char(COLOR))
                x, y = struct.unpack("<HH", raw[:4])
                st["rgb"] = rgb_to_hex(xy_to_rgb(x / 0xFFFF, y / 0xFFFF))
            except Exception:
                pass
        st.setdefault("mode", self.state.get("mode") or "ct")
        return st

    def _on_notify(self, char, data):
        u = str(char.uuid).lower()
        data = bytes(data)
        try:
            if u == POWER:
                self._set_light(on=bool(data[0]))
            elif u == BRIGHTNESS:
                self._set_light(bri=round(data[0] / 254 * 100))
            elif u == TEMPERATURE and len(data) >= 2:
                mired = struct.unpack("<H", data[:2])[0]
                if 100 <= mired <= 1000:
                    self._set_light(ct=round(1_000_000 / mired / 50) * 50)
            elif u == COLOR and len(data) >= 4:
                x, y = struct.unpack("<HH", data[:4])
                self._set_light(rgb=rgb_to_hex(xy_to_rgb(x / 0xFFFF, y / 0xFFFF)))
        except Exception:
            log.debug("notify parse", exc_info=True)

    async def _disconnect(self):
        c = self._client
        self._client = None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass
        self._set(connected=False, busy=False)
        log.info("connection released")

    async def _watchdog(self):
        while True:
            await asyncio.sleep(2)
            try:
                cfg = self._cfg().get("connection", {})
                mode = cfg.get("mode", "share")
                connected = self._client is not None and self._client.is_connected
                if connected and mode == "share" and not self._holds and not self._pending \
                        and (self._auto_task is None or self._auto_task.done()) \
                        and time.monotonic() - self._last_activity > max(3, int(cfg.get("idle_seconds", 20))):
                    await self._disconnect()
                elif not connected and mode == "keep" and self.address and not self.state["busy"] \
                        and time.monotonic() - self._last_fail > 20:
                    await self._ensure()
            except Exception:
                log.exception("watchdog")

    # ---------- writing ----------

    async def _write(self, key, value):
        c = self._client
        if c is None or not c.is_connected:
            raise ConnectionError("not connected")
        if key == "on":
            await c.write_gatt_char(POWER, bytes([1 if value else 0]), response=True)
            self._set_light(on=bool(value))
        elif key == "bri":
            raw = max(1, min(254, round(value * 2.54)))
            await c.write_gatt_char(BRIGHTNESS, bytes([raw]), response=True)
            self._set_light(bri=max(1, round(value)))
        elif key == "ct":
            mired = max(MIRED_MIN, min(MIRED_MAX, round(1_000_000 / value)))
            await c.write_gatt_char(TEMPERATURE, struct.pack("<H", mired), response=True)
            self._set_light(ct=int(value), mode="ct")
        elif key == "rgb":
            x, y = rgb_to_xy(*hex_to_rgb(value))
            await c.write_gatt_char(COLOR, struct.pack("<HH", round(x * 0xFFFF), round(y * 0xFFFF)), response=True)
            self._set_light(rgb=value, mode="xy")
        self._touch()

    def _enqueue(self, items):
        for k, v in items:
            self._pending.pop(k, None)  # re-append the new value at the end (keeps ordering)
            self._pending[k] = v
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = self._loop.create_task(self._writer())

    async def _writer(self):
        while self._pending:
            key = next(iter(self._pending))
            value = self._pending.pop(key)
            for attempt in range(2):
                if not await self._ensure():
                    self._pending.clear()
                    return
                try:
                    await self._write(key, value)
                    break
                except Exception as e:
                    log.warning("write error %s=%r (attempt %d): %r", key, value, attempt + 1, e)
                    await self._disconnect()
                    if attempt == 1:
                        self._set(error=friendly_error(e))
                        self._pending.clear()
                        return

    def _cancel_auto(self):
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()
        self._auto_task = None
        self._fx_end()

    def _do(self, items, cancel_auto=True):
        def go():
            if cancel_auto:
                self._cancel_auto()
            self._enqueue(items)
        self._call_soon(go)

    # ---------- public API (callable from any thread) ----------

    def connect(self):
        self._submit(self._ensure())

    def refresh(self):
        async def go():
            if await self._ensure():
                try:
                    self._set(**await self._read_all(self.state["has_color"], self.state["has_ct"]))
                except Exception as e:
                    log.warning("read: %r", e)
        self._submit(go())

    def wait_connected(self, timeout=25):
        try:
            return self._submit(self._ensure()).result(timeout)
        except Exception:
            return False

    def disconnect(self):
        self._submit(self._disconnect())

    def reconnect(self):
        async def go():
            await self._disconnect()
            self._last_fail = 0.0
            await self._ensure()
        self._submit(go())

    def set_power(self, on):
        self._do([("on", bool(on))])

    def toggle(self):
        async def go():
            self._cancel_auto()
            if await self._ensure():
                self._enqueue([("on", not self.state["on"])])
        self._submit(go())

    def set_brightness(self, pct):
        pct = max(1, min(100, float(pct)))
        items = [("bri", pct)]
        if not self.state.get("on"):
            items.append(("on", True))
        self._do(items)

    def step_brightness(self, delta):
        async def go():
            self._cancel_auto()
            if await self._ensure():
                cur = self.state["bri"] or 50
                self._enqueue([("bri", max(1, min(100, cur + delta))), ("on", True)])
        self._submit(go())

    def set_temperature(self, kelvin):
        items = [("ct", int(kelvin))]
        if not self.state.get("on"):
            items.append(("on", True))
        self._do(items)

    def set_color(self, hex_color):
        items = [("rgb", hex_color)]
        if not self.state.get("on"):
            items.append(("on", True))
        self._do(items)

    def apply_scene(self, scene):
        if not scene.get("on", True):
            self._do([("on", False)])
            return
        items = [("on", True)]
        if scene.get("mode") == "xy" and scene.get("color"):
            items.append(("rgb", scene["color"]))
        elif scene.get("ct"):
            items.append(("ct", int(scene["ct"])))
        if scene.get("bri"):
            items.append(("bri", float(scene["bri"])))
        self._do(items)

    def _start_auto(self, coro_fn, effect=None, fading=False):
        def go():
            self._cancel_auto()
            self._pending.clear()
            self._fx_shown, self._fx_shadow = set(), {}
            self._set(effect=effect, fading=fading)

            async def wrapper():
                try:
                    if await self._ensure():
                        await coro_fn()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning("auto task: %r", e)
                    self._set(error=friendly_error(e))
                finally:
                    if asyncio.current_task() is self._auto_task:
                        self._fx_end()
                        self._auto_task = None
                    self._touch()
            self._auto_task = self._loop.create_task(wrapper())
        self._call_soon(go)

    def fade(self, to_pct, minutes, from_low=False, kelvin=None, color=None):
        to_pct = max(0, min(100, float(to_pct)))
        seconds = max(1.0, float(minutes) * 60)

        async def run():
            if kelvin:
                await self._write("ct", int(kelvin))
            elif color:
                await self._write("rgb", color)
            if from_low or not self.state["on"]:
                start = 1.0
                await self._write("bri", 1)
                await self._write("on", True)
            else:
                start = float(self.state["bri"] or 1)
            steps = int(max(1, min(seconds / 1.5, 300)))
            for i in range(1, steps + 1):
                await asyncio.sleep(seconds / steps)
                v = start + (to_pct - start) * i / steps
                await self._write("bri", max(1, v))
            if to_pct <= 0:
                await self._write("on", False)
        self._start_auto(run, fading=True)

    def blink(self, times=3):
        async def run():
            was_on = bool(self.state["on"])
            for _ in range(int(times)):
                await self._write("on", not was_on)
                await asyncio.sleep(0.45)
                await self._write("on", was_on)
                await asyncio.sleep(0.45)
        self._start_auto(run)

    def start_effect(self, name):
        async def candle():
            await self._write("ct", 2200)
            await self._write("on", True)
            base = 45.0
            while True:
                base += random.uniform(-4, 4)
                base = max(25, min(60, base))
                await self._write("bri", max(5, base + random.uniform(-12, 6)))
                await asyncio.sleep(random.uniform(0.08, 0.35))

        async def breathe():
            await self._write("on", True)
            t0 = time.monotonic()
            while True:
                phase = (time.monotonic() - t0) / 7 * 2 * math.pi
                await self._write("bri", 8 + 92 * (0.5 - 0.5 * math.cos(phase)))
                await asyncio.sleep(0.12)

        async def colorloop():
            await self._write("on", True)
            h = random.random()
            while True:
                h = (h + 0.004) % 1
                await self._write("rgb", rgb_to_hex(c * 255 for c in colorsys.hsv_to_rgb(h, 1, 1)))
                await asyncio.sleep(0.25)

        async def party():
            await self._write("on", True)
            await self._write("bri", 100)
            h = random.random()
            while True:
                h = (h + random.uniform(0.2, 0.5)) % 1
                await self._write("rgb", rgb_to_hex(c * 255 for c in colorsys.hsv_to_rgb(h, 1, 1)))
                await asyncio.sleep(0.6)

        async def fire():
            await self._write("on", True)
            while True:
                h = random.uniform(0.0, 0.08)
                await self._write("rgb", rgb_to_hex(c * 255 for c in colorsys.hsv_to_rgb(h, 1, 1)))
                await self._write("bri", random.uniform(35, 90))
                await asyncio.sleep(random.uniform(0.1, 0.3))

        effects = {"candle": candle, "breathe": breathe, "colorloop": colorloop, "party": party, "fire": fire}
        if name in effects:
            self._start_auto(effects[name], effect=name)

    def start_stream(self, name, sample_fn, interval=0.25):
        """Stream a continuous source (e.g. screen color) to the light. sample_fn runs in a worker thread and
        returns a {'rgb': ..., 'ct': ..., 'bri': ...} dict (or None)."""
        async def run():
            await self._write("on", True)
            while True:
                t0 = time.monotonic()
                try:
                    res = await self._loop.run_in_executor(None, sample_fn)
                except Exception:
                    log.debug("stream sampling", exc_info=True)
                    res = None
                for k in ("ct", "rgb", "bri"):
                    if res and res.get(k) is not None:
                        await self._write(k, res[k])
                await asyncio.sleep(max(0.03, interval - (time.monotonic() - t0)))
        self._start_auto(run, effect=name)

    def set_values(self, ct=None, rgb=None, bri=None):
        """Write several values at once (does not turn the light on)."""
        items = []
        if rgb:
            items.append(("rgb", rgb))
        elif ct:
            items.append(("ct", int(ct)))
        if bri is not None:
            items.append(("bri", max(1, min(100, float(bri)))))
        if items:
            self._do(items)

    def stop_auto(self):
        self._call_soon(self._cancel_auto)

    # ---------- scanning / pairing ----------

    def scan(self, timeout=6):
        async def go():
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
            out = []
            for addr, (dev, adv) in found.items():
                name = adv.local_name or dev.name or ""
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                if "hue" in name.lower() or PHILIPS_UUID in uuids or SERVICE in uuids:
                    out.append({"address": addr, "name": name or "Hue bulb", "rssi": adv.rssi})
            out.sort(key=lambda d: -(d["rssi"] or -999))
            return out
        return self._submit(go()).result(timeout + 10)

    def pair(self, address):
        async def go():
            client = BleakClient(address, timeout=20)
            try:
                await client.connect()
                await client.pair()
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": friendly_error(e)}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return self._submit(go()).result(60)

    def switch_device(self):
        """Settings changed: drop the old device and connect to the new one."""
        async def go():
            self._cancel_auto()
            await self._disconnect()
            self._set(on=None, bri=None, ct=None, rgb=None, error=None)
            self._last_fail = 0.0
            await self._ensure()
        self._submit(go())
