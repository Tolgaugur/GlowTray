"""Usage statistics: time on, hour-of-day distribution, estimated energy, most used scenes/effects."""
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from config import APP_DIR

log = logging.getLogger("glowtray.stats")
STATS_PATH = os.path.join(APP_DIR, "stats.json")
TICK = 10          # s
MAX_GAP = 60       # a longer gap means sleep/off and is not counted
KEEP_DAYS = 400
DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _empty_day():
    return {"on": 0.0, "bri": 0.0, "wh": 0.0, "hours": [0.0] * 24, "ons": 0}


class Stats(threading.Thread):
    def __init__(self, app):
        super().__init__(name="stats", daemon=True)
        self.app = app
        self._lock = threading.Lock()
        self.data = {"since": date.today().isoformat(), "days": {}, "scenes": {}, "effects": {}}
        self._load()
        self._last = None
        self._was_on = None
        self._dirty = False
        self._last_save = time.monotonic()

    def _load(self):
        try:
            with open(STATS_PATH, encoding="utf-8") as f:
                d = json.load(f)
            self.data.update({k: d[k] for k in ("since", "days", "scenes", "effects") if k in d})
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read stats")

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            self.data["days"] = {k: v for k, v in self.data["days"].items() if k >= cutoff}
            payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
            self._dirty = False
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            tmp = STATS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, STATS_PATH)
        except Exception:
            log.exception("could not save stats")
        self._last_save = time.monotonic()

    def _day(self, d=None):
        key = (d or date.today()).isoformat()
        return self.data["days"].setdefault(key, _empty_day())

    def count(self, kind, key):
        """kind: 'scenes' | 'effects'"""
        if not key:
            return
        with self._lock:
            bucket = self.data.setdefault(kind, {})
            bucket[key] = bucket.get(key, 0) + 1
            self._dirty = True

    def run(self):
        while True:
            time.sleep(TICK)
            try:
                self._tick()
                if time.monotonic() - self._last_save > 120:
                    self.save()
            except Exception:
                log.exception("stats tick")

    def _tick(self):
        now = time.time()
        dt = 0.0 if self._last is None else now - self._last
        self._last = now
        if dt > MAX_GAP:
            dt = 0.0
        st = self.app.light.snapshot()
        on = st.get("on")
        if on is None:
            return
        watts = float(self.app.cfg.get().get("stats", {}).get("watts", 9) or 9)
        with self._lock:
            d = self._day()
            if on and self._was_on is False:
                d["ons"] += 1
            self._was_on = on
            if on and dt:
                bri = float(st.get("bri") or 100)
                d["on"] += dt
                d["bri"] += bri * dt
                d["wh"] += watts * max(0.1, bri / 100) * dt / 3600
                d["hours"][datetime.now().hour] += dt
                self._dirty = True

    # ---------- summary for the UI ----------

    def summary(self):
        cfg = self.app.cfg.get()
        price = float(cfg.get("stats", {}).get("price", 0) or 0)
        today = date.today()
        with self._lock:
            days = {k: dict(v) for k, v in self.data["days"].items()}
            scenes = dict(self.data.get("scenes", {}))
            effects = dict(self.data.get("effects", {}))
            since = self.data.get("since")

        def get(d):
            return days.get(d.isoformat()) or _empty_day()

        def series(n):
            out = []
            for i in range(n - 1, -1, -1):
                d = today - timedelta(days=i)
                v = get(d)
                out.append({"date": d.isoformat(), "label": DAY_NAMES[d.weekday()] if n <= 7 else str(d.day),
                            "on": round(v["on"]), "wh": round(v["wh"], 2)})
            return out

        def total(n):
            on = wh = bri = ons = 0.0
            hours = [0.0] * 24
            for i in range(n):
                v = get(today - timedelta(days=i))
                on += v["on"]
                wh += v["wh"]
                bri += v["bri"]
                ons += v.get("ons", 0)
                hours = [a + b for a, b in zip(hours, v["hours"])]
            return {"on": round(on), "wh": round(wh, 2), "avg_bri": round(bri / on) if on else None,
                    "ons": int(ons), "hours": [round(h) for h in hours]}

        week, month = total(7), total(30)
        active_days = sum(1 for i in range(30) if get(today - timedelta(days=i))["on"] > 0)
        name_of = {s["id"]: s["name"] for s in cfg.get("scenes", [])}
        top_scenes = sorted(((name_of.get(k, "Deleted scene"), v) for k, v in scenes.items()), key=lambda x: -x[1])[:5]
        return {
            "since": since,
            "today": total(1),
            "week": week,
            "month": month,
            "daily_avg": round(month["on"] / active_days) if active_days else 0,
            "series7": series(7),
            "series30": series(30),
            "top_scenes": top_scenes,
            "top_effects": sorted(effects.items(), key=lambda x: -x[1])[:5],
            "price": price,
            "watts": float(cfg.get("stats", {}).get("watts", 9) or 9),
        }

    def reset(self):
        with self._lock:
            self.data = {"since": date.today().isoformat(), "days": {}, "scenes": {}, "effects": {}}
            self._dirty = True
        self.save()
