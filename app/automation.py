"""Rule engine: trigger + conditions -> action. Also the countdown timer."""
import logging
import queue
import threading
import time
from datetime import date, datetime, timedelta

import psutil

import sun
import winutil

log = logging.getLogger("glowtray.auto")

EVENT_TRIGGERS = {"lock", "unlock", "sleep", "resume", "startup", "shutdown"}


def _hm(s):
    h, m = (int(x) for x in str(s).split(":")[:2])
    return h * 60 + m


class Automation:
    def __init__(self, app):
        self.app = app
        self._q = queue.Queue()
        self._fired = {}        # rule id -> key of the last firing
        self._idle_fired = set()
        self._max_idle = 0.0
        self._procs = None
        self._procs_ts = 0.0
        self.timer = None       # {"end": epoch, "action": {...}, "minutes": n}
        self._sun_cache = {}    # (day, lat, lon) -> (sunrise, sunset)
        self._thread = threading.Thread(target=self._loop, name="automation", daemon=True)

    def start(self):
        self._thread.start()

    def emit(self, name, **data):
        self._q.put((name, data))

    # ---------- timer ----------

    def start_timer(self, minutes, action):
        self.timer = {"end": time.time() + float(minutes) * 60, "action": action, "minutes": minutes}
        self.app.push_timer()

    def cancel_timer(self):
        self.timer = None
        self.app.push_timer()

    # ---------- helpers ----------

    def location(self):
        """(lat, lon), or None while the user hasn't picked a location."""
        loc = self.app.cfg.get()["location"]
        if loc.get("lat") is None or loc.get("lon") is None:
            return None
        return float(loc["lat"]), float(loc["lon"])

    def sun_on(self, day):
        loc = self.location()
        if loc is None:
            return None, None
        key = (day, *loc)
        if key not in self._sun_cache:
            if len(self._sun_cache) > 16:
                self._sun_cache.clear()
            self._sun_cache[key] = sun.sun_times(*loc, day)
        return self._sun_cache[key]

    def sun_today(self):
        return self.sun_on(date.today())

    def running_processes(self, max_age=3.0):
        if self._procs is None or time.monotonic() - self._procs_ts > max_age:
            names = set()
            for p in psutil.process_iter(["name"]):
                try:
                    if p.info["name"]:
                        names.add(p.info["name"].lower())
                except Exception:
                    pass
            self._procs, self._procs_ts = names, time.monotonic()
        return self._procs

    @staticmethod
    def _norm_proc(name):
        name = (name or "").strip().lower()
        if name and not name.endswith(".exe"):
            name += ".exe"
        return name

    def _uses_processes(self, rules):
        for r in rules:
            if not r.get("enabled"):
                continue
            if r["trigger"].get("type") in ("process_start", "process_stop"):
                return True
            if any(c.get("type") == "process" for c in r.get("conditions", [])):
                return True
        return False

    # ---------- conditions ----------

    def check_condition(self, c, now):
        t = c.get("type")
        if t == "time_between":
            a, b, n = _hm(c.get("from", "00:00")), _hm(c.get("to", "23:59")), now.hour * 60 + now.minute
            return a <= n < b if a <= b else (n >= a or n < b)
        if t == "days":
            return now.weekday() in c.get("days", [])
        if t == "dark":
            loc = self.location()
            if loc is None:
                return False
            return sun.is_dark(*loc, now) == bool(c.get("value", True))
        if t == "light":
            st = self.app.light.snapshot()
            if st.get("on") is None:
                self.app.light.wait_connected(20)
                st = self.app.light.snapshot()
            want_on = c.get("value", "on") == "on"
            return st.get("on") is not None and bool(st.get("on")) == want_on
        if t == "process":
            running = self._norm_proc(c.get("name")) in self.running_processes()
            return running == bool(c.get("running", True))
        if t == "locked":
            return self.app.sysevents.locked == bool(c.get("value", True))
        if t == "idle":
            return winutil.idle_seconds() >= float(c.get("minutes", 5)) * 60
        return True

    def conditions_ok(self, rule, now):
        for c in rule.get("conditions", []):
            try:
                if not self.check_condition(c, now):
                    return False
            except Exception:
                log.exception("condition error %s", c)
                return False
        return True

    def fire(self, rule, now, reason=""):
        if not self.conditions_ok(rule, now):
            log.info("rule '%s' triggered but conditions not met (%s)", rule.get("name"), reason)
            return False
        log.info("running rule: %s (%s)", rule.get("name"), reason)
        self.app.run_action(rule.get("action", {}), source=rule.get("name"))
        return True

    # ---------- loop ----------

    def _loop(self):
        last_tick_min = None
        while True:
            try:
                try:
                    name, data = self._q.get(timeout=1)
                except queue.Empty:
                    name, data = None, None
                now = datetime.now()
                rules = [r for r in self.app.cfg.get().get("rules", []) if r.get("enabled")]

                if name is not None:
                    self._handle_event(name, data, rules, now)

                # Timer
                if self.timer and time.time() >= self.timer["end"]:
                    action = self.timer["action"]
                    self.timer = None
                    self.app.run_action(action, source="Timer")
                    self.app.push_timer()

                # Per-minute triggers (time / sun)
                minute_key = now.strftime("%Y%m%d%H%M")
                if minute_key != last_tick_min:
                    last_tick_min = minute_key
                    self._minute_triggers(rules, now, minute_key)

                self._idle_triggers(rules, now)
                if self._uses_processes(rules):
                    self._process_triggers(rules, now)
                else:
                    self._procs_prev = None
            except Exception:
                log.exception("automation loop")
                time.sleep(1)

    def _handle_event(self, name, data, rules, now):
        for r in rules:
            if r["trigger"].get("type") == name:
                threading.Thread(target=self.fire, args=(r, now, name), daemon=True).start()

    def _sun_due(self, tr, now, days):
        """True if the sun event plus offset falls on this minute.

        A large offset can push the target past midnight, so the events of yesterday and tomorrow are checked
        too, and the weekday filter applies to the day of the sun event itself.
        """
        offset = timedelta(minutes=int(tr.get("offset", 0)))
        minute = now.replace(second=0, microsecond=0)
        for d in (-1, 0, 1):
            day = now.date() + timedelta(days=d)
            if day.weekday() not in days:
                continue
            rise, set_ = self.sun_on(day)
            base = rise if tr.get("event") == "sunrise" else set_
            if base is not None and (base + offset).replace(second=0, microsecond=0) == minute:
                return True
        return False

    def _minute_triggers(self, rules, now, minute_key):
        hm = now.strftime("%H:%M")
        for r in rules:
            tr = r["trigger"]
            t = tr.get("type")
            days = tr.get("days", list(range(7)))
            if t == "time":
                due = now.weekday() in days and tr.get("time") == hm
            elif t == "sun":
                due = self._sun_due(tr, now, days)
            else:
                continue
            if due and self._fired.get(r["id"]) != minute_key:
                self._fired[r["id"]] = minute_key
                threading.Thread(target=self.fire, args=(r, now, t), daemon=True).start()

    def _idle_triggers(self, rules, now):
        idle = winutil.idle_seconds()
        for r in rules:
            tr = r["trigger"]
            if tr.get("type") == "idle":
                need = float(tr.get("minutes", 10)) * 60
                if idle >= need and r["id"] not in self._idle_fired:
                    self._idle_fired.add(r["id"])
                    threading.Thread(target=self.fire, args=(r, now, "idle"), daemon=True).start()
            elif tr.get("type") == "active":
                need = float(tr.get("minutes", 10)) * 60
                if idle < 3 and self._max_idle >= need:
                    threading.Thread(target=self.fire, args=(r, now, "active"), daemon=True).start()
        if idle < 3:
            self._idle_fired.clear()
            self._max_idle = 0.0
        else:
            self._max_idle = max(self._max_idle, idle)

    _procs_prev = None

    def _process_triggers(self, rules, now):
        if time.monotonic() - self._procs_ts < 3:
            return
        cur = self.running_processes(0)
        prev = self._procs_prev
        self._procs_prev = cur
        if prev is None:
            return
        for r in rules:
            tr = r["trigger"]
            t = tr.get("type")
            if t not in ("process_start", "process_stop"):
                continue
            n = self._norm_proc(tr.get("name"))
            if not n:
                continue
            if t == "process_start" and n in cur and n not in prev:
                threading.Thread(target=self.fire, args=(r, now, "process_start"), daemon=True).start()
            elif t == "process_stop" and n in prev and n not in cur:
                threading.Thread(target=self.fire, args=(r, now, "process_stop"), daemon=True).start()
