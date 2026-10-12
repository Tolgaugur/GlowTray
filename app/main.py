"""Glowtray — a Windows system tray app for Philips Hue Bluetooth bulbs."""
import collections
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime

import psutil
import pystray
import webview

import trayicon
import winutil
from automation import Automation
from config import LOG_PATH, Config, new_id
from fluxsync import FluxSync
from light import Light
from screen import ScreenSync, list_monitors
from stats import Stats
from sysevents import SysEvents

APP_NAME = "Glowtray"
VERSION = "1.2.0"
PANEL_W, PANEL_H = 396, 680  # the frameless window ends up with a ~380x640 client area
log = logging.getLogger("glowtray")

EFFECT_NAMES = {"candle": "Candle", "breathe": "Breathe", "colorloop": "Color loop", "party": "Party", "fire": "Fireplace",
                "screen": "Match screen"}


def resource(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def describe_action(a, scenes):
    t = a.get("type")
    if t == "on":
        return "Turn on"
    if t == "off":
        return "Turn off"
    if t == "toggle":
        return "Toggle"
    if t == "brightness":
        return f"Brightness {a.get('pct')}%"
    if t == "temperature":
        return f"White {a.get('kelvin')}K"
    if t == "color":
        return f"Color {a.get('color')}"
    if t == "scene":
        s = next((s for s in scenes if s["id"] == a.get("scene")), None)
        return f"Scene: {s['name'] if s else '?'}"
    if t == "fade":
        return f"Fade to {a.get('to')}% ({a.get('minutes')} min)"
    if t == "effect":
        return f"Effect: {EFFECT_NAMES.get(a.get('effect'), a.get('effect'))}"
    if t == "stop_effect":
        return "Stop effect"
    if t == "blink":
        return "Blink"
    if t == "notify":
        return "Notification"
    if t == "flux":
        return "f.lux sync " + ("on" if a.get("on", True) else "off")
    return t or "?"


class Api:
    """Methods called from the UI (JavaScript)."""

    def __init__(self, app):
        self._app = app

    # light
    def init(self):
        return self._app.init_payload()

    def power(self, on):
        self._app.light.set_power(bool(on))

    def toggle(self):
        self._app.light.toggle()

    def brightness(self, pct):
        self._app.flux.manual("bri")
        self._app.light.set_brightness(pct)

    def temperature(self, kelvin):
        self._app.flux.manual("color")
        self._app.light.set_temperature(int(kelvin))

    def color(self, hex_color):
        self._app.flux.manual("color")
        self._app.light.set_color(hex_color)

    def effect(self, name):
        self._app.start_effect(name)

    def effect_stop(self):
        self._app.light.stop_auto()

    def reconnect(self):
        self._app.light.reconnect()

    def disconnect(self):
        self._app.light.disconnect()

    # scenes
    def scene_apply(self, sid):
        s = self._app.find_scene(sid)
        if s:
            self._app.apply_scene(s)

    def scene_save(self, name, sid=None):
        st = self._app.light.snapshot()
        scene = {"id": sid or new_id(), "name": (name or "Scene").strip()[:24], "on": True,
                 "bri": st.get("bri") or 100, "mode": st.get("mode") or "ct"}
        if scene["mode"] == "xy":
            scene["color"] = st.get("rgb") or "#ffffff"
        else:
            scene["ct"] = st.get("ct") or 2700
        scenes = [s for s in self._app.cfg.get()["scenes"] if s["id"] != scene["id"]]
        scenes.append(scene)
        self._app.cfg.set("scenes", scenes)
        return scenes

    def scene_delete(self, sid):
        scenes = [s for s in self._app.cfg.get()["scenes"] if s["id"] != sid]
        self._app.cfg.set("scenes", scenes)
        return scenes

    def scenes_reorder(self, ids):
        scenes = {s["id"]: s for s in self._app.cfg.get()["scenes"]}
        self._app.cfg.set("scenes", [scenes[i] for i in ids if i in scenes])

    # timer
    def timer_start(self, minutes, action):
        self._app.auto.start_timer(float(minutes), action)
        self._app.cfg.set("timer_action", action.get("type", "off"))
        return self._app.timer_payload()

    def timer_cancel(self):
        self._app.auto.cancel_timer()

    # rules
    def rule_save(self, rule):
        rule = dict(rule)
        rule.setdefault("id", new_id())
        if not rule.get("id"):
            rule["id"] = new_id()
        rules = self._app.cfg.get()["rules"]
        for i, r in enumerate(rules):
            if r["id"] == rule["id"]:
                rules[i] = rule
                break
        else:
            rules.append(rule)
        self._app.cfg.set("rules", rules)
        return rules

    def rule_delete(self, rid):
        rules = [r for r in self._app.cfg.get()["rules"] if r["id"] != rid]
        self._app.cfg.set("rules", rules)
        return rules

    def rule_enable(self, rid, enabled):
        rules = self._app.cfg.get()["rules"]
        for r in rules:
            if r["id"] == rid:
                r["enabled"] = bool(enabled)
        self._app.cfg.set("rules", rules)
        return rules

    def rule_test(self, rule):
        """Run the rule now; also report whether its conditions currently hold."""
        now = datetime.now()
        ok = self._app.auto.conditions_ok(rule, now)
        self._app.run_action(rule.get("action", {}), source=f"Test: {rule.get('name', '')}")
        return {"conditions_ok": ok}

    def preview_action(self, action):
        self._app.run_action(action, source="Preview")

    # settings
    def settings(self, patch):
        return self._app.apply_settings(patch)

    def scan(self):
        try:
            return {"ok": True, "devices": self._app.light.scan()}
        except Exception as e:
            log.exception("scan")
            return {"ok": False, "error": str(e)}

    def pair(self, address):
        return self._app.light.pair(address)

    def sun(self, lat, lon):
        import sun as _sun
        rise, set_ = _sun.sun_times(float(lat), float(lon))
        return {"sunrise": rise.strftime("%H:%M") if rise else None, "sunset": set_.strftime("%H:%M") if set_ else None}

    def processes(self):
        return sorted(self._app.auto.running_processes(0))

    def hotkey_capture(self, active):
        if active:
            self._app.sysevents.set_hotkeys({})
        else:
            self._app.sysevents.set_hotkeys(self._app.cfg.get()["hotkeys"])
            time.sleep(0.3)
            return self._app.sysevents.hotkey_errors

    def monitors(self):
        return [{"id": m["id"], "label": m["label"]} for m in list_monitors()]

    def flux_resume(self):
        self._app.flux.resume()

    def stats(self):
        return self._app.stats.summary()

    def stats_reset(self):
        self._app.stats.reset()
        return self._app.stats.summary()

    def pin(self, pinned):
        self._app.pinned = bool(pinned)

    def hide(self):
        self._app.hide_panel()

    def open_log(self):
        os.startfile(os.path.dirname(LOG_PATH))

    def quit(self):
        self._app.quit()


class App:
    def __init__(self, start_hidden):
        self.start_hidden = start_hidden
        self.cfg = Config()
        self.light = Light(self.cfg.get, self._on_light)
        self.sysevents = SysEvents(self._on_sys)
        self.auto = Automation(self)
        self.flux = FluxSync(self)
        self.stats = Stats(self)
        self.window = None
        self.tray = None
        self.hwnd = None
        self.visible = False
        self.pinned = False
        self.quitting = False
        self._last_hide = 0.0
        self._loaded = threading.Event()
        self._push_evt = threading.Event()
        self._latest = self.light.snapshot()
        self._icon_key = None
        self._pids = (0.0, {os.getpid()})
        self.activity = collections.deque(maxlen=40)

    # ---------- lifecycle ----------

    def run(self):
        with open(resource(os.path.join("ui", "index.html")), encoding="utf-8") as f:
            html = f.read()
        dark = winutil.system_dark_mode() if self.cfg.get()["theme"] == "system" else self.cfg.get()["theme"] == "dark"
        self.window = webview.create_window(
            APP_NAME, html=html, js_api=Api(self), width=PANEL_W, height=PANEL_H,
            resizable=False, frameless=True, easy_drag=False, on_top=True, hidden=True,
            shadow=True, background_color="#202020" if dark else "#f3f3f3")
        self.window.events.before_show += self._before_show
        self.window.events.closing += self._on_closing
        self.window.events.loaded += lambda: self._loaded.set()
        webview.start(self._started, private_mode=True)

    def _before_show(self):
        self.hwnd = winutil.hwnd_of(self.window)
        if self.hwnd:
            winutil.make_tool_window(self.hwnd)
            winutil.round_corners(self.hwnd)

    def _on_closing(self):
        if self.quitting:
            return True
        threading.Thread(target=self.hide_panel, daemon=True).start()
        return False

    def _started(self):
        cfg = self.cfg.get()
        self.light.start()
        self.sysevents.start()
        self.sysevents.set_hotkeys(cfg["hotkeys"])
        self.auto.start()
        self.flux.start()
        self.stats.start()
        threading.Thread(target=self._push_loop, name="push", daemon=True).start()
        self._start_tray()

        if getattr(sys, "frozen", False):
            winutil.set_autostart(cfg["autostart"])
        if cfg["connection"]["mode"] == "keep":
            self.light.connect()

        def later():
            time.sleep(3)
            self.auto.emit("startup")
            if cfg.get("first_run"):
                self.notify(f"{APP_NAME} is running in the system tray. Click the bulb icon to open the panel.")
                self.cfg.set("first_run", False)
            if getattr(sys, "frozen", False) and not cfg.get("tray_promoted"):
                time.sleep(2)
                if winutil.promote_tray_icon():
                    self.cfg.set("tray_promoted", True)
                    try:
                        self.tray.visible = False
                        self.tray.visible = True
                    except Exception:
                        pass
        threading.Thread(target=later, daemon=True).start()

        if self.start_hidden:
            self._loaded.wait(10)
            self._webview_memory(low=True)
        else:
            self._loaded.wait(10)
            time.sleep(0.2)
            self.show_panel()

    def quit(self):
        self.quitting = True
        try:
            self.stats.save()
        except Exception:
            pass
        try:
            self.light.disconnect()
            time.sleep(0.5)
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---------- tray ----------

    def _start_tray(self):
        def scenes_menu():
            return [pystray.MenuItem(s["name"], (lambda s: lambda: self.apply_scene(s))(s))
                    for s in self.cfg.get()["scenes"]]

        def toggle_label(_):
            return "Turn light off" if self.light.snapshot().get("on") else "Turn light on"

        menu = pystray.Menu(
            pystray.MenuItem("Open panel", lambda: threading.Thread(target=self.toggle_panel, daemon=True).start(),
                             default=True),
            pystray.MenuItem(toggle_label, lambda: self.light.toggle()),
            pystray.MenuItem("Scenes", pystray.Menu(scenes_menu)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.quit()),
        )
        self.tray = pystray.Icon("Glowtray", trayicon.icon_for_state(self._latest), APP_NAME, menu)
        threading.Thread(target=self.tray.run, name="tray", daemon=True).start()

    def _update_tray(self, st):
        key = (st.get("on"), st.get("mode"), st.get("rgb"), st.get("ct"), st.get("bri"), st.get("connected"),
               st.get("error"))
        if key == self._icon_key or self.tray is None:
            return
        self._icon_key = key
        try:
            self.tray.icon = trayicon.icon_for_state(st)
            if st.get("on") is None:
                title = f"{APP_NAME} — status unknown"
            elif st.get("on"):
                title = f"{APP_NAME} — On, {st.get('bri')}%"
            else:
                title = f"{APP_NAME} — Off"
            self.tray.title = title[:120]
        except Exception:
            log.exception("tray update")

    def notify(self, text, title=APP_NAME):
        try:
            self.tray.notify(text, title)
        except Exception:
            log.exception("notify")

    # ---------- panel ----------

    def toggle_panel(self):
        if self.visible:
            self.hide_panel()
        elif time.monotonic() - self._last_hide > 0.45:
            self.show_panel()

    def show_panel(self):
        if self.visible or not self.hwnd:
            return
        self.visible = True
        self.light.hold("panel")
        self._webview_memory(low=False)
        self.light.refresh()
        winutil.place_near_tray(self.hwnd)
        self.js("hue.onShow()")
        self.window.show()
        winutil.force_foreground(self.hwnd)
        threading.Thread(target=self._watch_focus, daemon=True).start()

    def hide_panel(self):
        if not self.visible:
            return
        self.visible = False
        self._last_hide = time.monotonic()
        self.window.hide()
        self.light.release("panel")
        self._webview_memory(low=True)
        self.js("hue.onHide()")

    def _webview_memory(self, low):
        """Ask WebView2 to trim its memory while the panel is hidden (~40 MB)."""
        try:
            import System
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
            form = self.window.native

            def apply():
                core = form.webview.CoreWebView2
                if core is not None:
                    core.MemoryUsageTargetLevel = Level.Low if low else Level.Normal
                return None
            form.Invoke(System.Func[System.Object](apply))
        except Exception:
            log.debug("webview memory", exc_info=True)

    def _our_pids(self):
        ts, pids = self._pids
        if time.monotonic() - ts > 5:
            pids = {os.getpid()}
            try:
                pids |= {p.pid for p in psutil.Process().children(recursive=True)}
            except Exception:
                pass
            self._pids = (time.monotonic(), pids)
        return pids

    def _watch_focus(self):
        time.sleep(0.6)
        misses = 0
        while self.visible:
            if not self.pinned:
                pid, root = winutil.foreground_pid()
                ours = (root == self.hwnd) or (pid in self._our_pids())
                misses = 0 if ours else misses + 1
                if misses >= 2:
                    self.hide_panel()
                    return
            time.sleep(0.1)

    # ---------- pushing data to the UI ----------

    def js(self, code):
        if not self._loaded.is_set():
            return
        try:
            self.window.evaluate_js(f"window.hue && {code}")
        except Exception:
            log.debug("js", exc_info=True)

    def _on_light(self, st):
        self._latest = st
        self._push_evt.set()

    def _push_loop(self):
        while True:
            self._push_evt.wait()
            self._push_evt.clear()
            st = self._latest
            self._update_tray(st)
            self.js(f"hue.onState({json.dumps(st)})")
            time.sleep(0.04)

    def timer_payload(self):
        t = self.auto.timer
        if not t:
            return None
        return {"end": t["end"] * 1000, "action": t["action"], "minutes": t["minutes"]}

    def push_flux(self, st):
        self.js(f"hue.onFlux({json.dumps(st)})")

    def push_timer(self):
        self.js(f"hue.onTimer({json.dumps(self.timer_payload())})")

    def log_activity(self, source, text):
        item = {"time": datetime.now().strftime("%H:%M"), "source": source, "text": text}
        self.activity.appendleft(item)
        self.js(f"hue.onActivity({json.dumps(list(self.activity))})")

    def init_payload(self):
        rise, set_ = self.auto.sun_today()
        return {
            "config": self.cfg.get(),
            "state": self.light.snapshot(),
            "timer": self.timer_payload(),
            "sun": {"sunrise": rise.strftime("%H:%M") if rise else None,
                    "sunset": set_.strftime("%H:%M") if set_ else None},
            "version": VERSION,
            "autostart": winutil.get_autostart(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "systemDark": winutil.system_dark_mode(),
            "hotkeyErrors": self.sysevents.hotkey_errors,
            "activity": list(self.activity),
            "pinned": self.pinned,
            "flux": self.flux.status,
        }

    # ---------- actions ----------

    def find_scene(self, sid):
        return next((s for s in self.cfg.get()["scenes"] if s["id"] == sid), None)

    def apply_scene(self, scene):
        self.flux.manual("color")
        self.stats.count("scenes", scene.get("id"))
        self.light.apply_scene(scene)

    def start_effect(self, name):
        self.flux.manual("color")
        self.stats.count("effects", name)
        if name == "screen":
            sync = ScreenSync(lambda: self.cfg.get()["screen"])
            self.light.start_stream("screen", sync.sample, 0.25)
        else:
            self.light.start_effect(name)

    def run_action(self, a, source=""):
        t = a.get("type")
        L = self.light
        try:
            if t == "on":
                L.set_power(True)
            elif t == "off":
                L.set_power(False)
            elif t == "toggle":
                L.toggle()
            elif t == "brightness":
                self.flux.manual("bri")
                L.set_brightness(a.get("pct", 100))
            elif t == "temperature":
                self.flux.manual("color")
                L.set_temperature(a.get("kelvin", 2700))
            elif t == "color":
                self.flux.manual("color")
                L.set_color(a.get("color", "#ffffff"))
            elif t == "scene":
                s = self.find_scene(a.get("scene"))
                if s:
                    self.apply_scene(s)
            elif t == "fade":
                self.flux.manual("color" if a.get("kelvin") or a.get("color") else "bri")
                L.fade(a.get("to", 100), a.get("minutes", 10), bool(a.get("from_low")),
                       kelvin=a.get("kelvin") or None, color=a.get("color") or None)
            elif t == "effect":
                self.start_effect(a.get("effect", "candle"))
            elif t == "flux":
                self.apply_settings({"flux": {"enabled": bool(a.get("on", True))}})
                self.js(f"hue.onConfig({json.dumps(self.cfg.get())})")
            elif t == "stop_effect":
                L.stop_auto()
            elif t == "blink":
                L.blink(int(a.get("times", 3)))
            elif t == "notify":
                self.notify(a.get("text") or APP_NAME, source or APP_NAME)
            self.log_activity(source or "?", describe_action(a, self.cfg.get()["scenes"]))
        except Exception:
            log.exception("action %s", a)

    def apply_settings(self, patch):
        old = self.cfg.get()
        new = self.cfg.update(patch)
        if "autostart" in patch:
            winutil.set_autostart(bool(patch["autostart"]))
        if "hotkeys" in patch:
            self.sysevents.set_hotkeys(new["hotkeys"])
            time.sleep(0.3)
        if "device" in patch and patch["device"].get("address") != old["device"].get("address"):
            self.light.switch_device()
        if "connection" in patch and new["connection"]["mode"] == "keep":
            self.light.connect()
        if "flux" in patch:
            if patch["flux"].get("enabled"):
                self.flux.resume()
            else:
                self.flux.poke()
        return {"config": new, "hotkeyErrors": self.sysevents.hotkey_errors, "autostart": winutil.get_autostart()}

    # ---------- system events ----------

    def _on_sys(self, name, data):
        if name == "hotkey":
            h = data.get("name")
            if h == "toggle":
                self.light.toggle()
            elif h == "panel":
                threading.Thread(target=self.toggle_panel, daemon=True).start()
            elif h == "bri_up":
                self.flux.manual("bri")
                self.light.step_brightness(10)
            elif h == "bri_down":
                self.flux.manual("bri")
                self.light.step_brightness(-10)
            return
        if name == "show_panel":
            threading.Thread(target=self.show_panel, daemon=True).start()
            return
        log.info("system event: %s", name)
        if name in ("sleep", "shutdown"):
            self.stats.save()
        self.auto.emit(name)


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if not getattr(sys, "frozen", False):
        root.addHandler(logging.StreamHandler())
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("pywebview").setLevel(logging.WARNING)


def main():
    setup_logging()
    # A simple panel does not need GPU acceleration; WebView2 uses ~100 MB less memory without it
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu --disable-gpu-compositing")
    if not winutil.single_instance():
        # Already running: open the panel of the existing instance
        if "--tray" not in sys.argv:
            winutil.signal_existing()
        log.info("second instance, exiting")
        return
    log.info("starting v%s", VERSION)
    App(start_hidden="--tray" in sys.argv).run()


if __name__ == "__main__":
    main()
