"""Settings, stored in %APPDATA%\\Glowtray\\config.json."""
import copy
import json
import os
import shutil
import threading
import uuid

_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DIR = os.path.join(_APPDATA, "Glowtray")
LEGACY_DIR = os.path.join(_APPDATA, "HueKontrol")  # settings folder used before the rename
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "glowtray.log")


def new_id():
    return uuid.uuid4().hex[:10]


DEFAULTS = {
    "device": {"address": "", "name": ""},
    "connection": {"mode": "share", "idle_seconds": 20},
    "theme": "system",
    "autostart": False,
    "hotkeys": {"toggle": "Ctrl+Alt+L", "panel": "Ctrl+Alt+H", "bri_up": "", "bri_down": ""},
    "location": {"city": "", "lat": None, "lon": None},  # unset until the user picks one
    "scenes": [
        {"id": "s_read", "name": "Read", "on": True, "bri": 100, "mode": "ct", "ct": 4000},
        {"id": "s_relax", "name": "Relax", "on": True, "bri": 55, "mode": "ct", "ct": 2700},
        {"id": "s_focus", "name": "Focus", "on": True, "bri": 100, "mode": "ct", "ct": 5500},
        {"id": "s_night", "name": "Night", "on": True, "bri": 8, "mode": "ct", "ct": 2200},
        {"id": "s_movie", "name": "Movie", "on": True, "bri": 18, "mode": "xy", "color": "#4b3cff"},
        {"id": "s_sunset", "name": "Sunset", "on": True, "bri": 70, "mode": "xy", "color": "#ff7a2f"},
    ],
    "rules": [
        {
            "id": "r_sunset", "name": "Turn on at sunset", "enabled": False,
            "trigger": {"type": "sun", "event": "sunset", "offset": -15, "days": [0, 1, 2, 3, 4, 5, 6]},
            "conditions": [{"type": "locked", "value": False}],
            "action": {"type": "scene", "scene": "s_relax"},
        },
        {
            "id": "r_wake", "name": "Wake-up light", "enabled": False,
            "trigger": {"type": "time", "time": "07:30", "days": [0, 1, 2, 3, 4]},
            "conditions": [],
            "action": {"type": "fade", "to": 100, "minutes": 15, "from_low": True, "kelvin": 4000},
        },
        {
            "id": "r_lock", "name": "Turn off when PC locks", "enabled": False,
            "trigger": {"type": "lock"},
            "conditions": [{"type": "light", "value": "on"}],
            "action": {"type": "off"},
        },
    ],
    "screen": {"monitor": "", "follow_bri": True, "min_bri": 5, "max_bri": 100},
    "flux": {"enabled": False, "dim": True, "min_bri": 30, "max_bri": 100},
    "stats": {"watts": 9, "price": 0},
    "timer_action": "off",
    "first_run": True,
    "tray_promoted": False,
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate_legacy():
    """Carry over settings and stats from the pre-rename folder on first start."""
    # The log file may already have created APP_DIR, so check for the config file itself
    if os.path.exists(CONFIG_PATH) or not os.path.isdir(LEGACY_DIR):
        return
    os.makedirs(APP_DIR, exist_ok=True)
    for name in ("config.json", "stats.json"):
        src = os.path.join(LEGACY_DIR, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(APP_DIR, name))
            except OSError:
                pass


class Config:
    def __init__(self):
        self._lock = threading.RLock()
        _migrate_legacy()
        os.makedirs(APP_DIR, exist_ok=True)
        data = {}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            # Back up the corrupt file and continue with defaults
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".corrupt")
            except OSError:
                pass
        self.data = _merge(DEFAULTS, data)
        self.save()

    def get(self):
        with self._lock:
            return copy.deepcopy(self.data)

    def update(self, patch):
        with self._lock:
            self.data = _merge(self.data, patch)
            self.save()
            return self.get()

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self.save()

    def save(self):
        with self._lock:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
