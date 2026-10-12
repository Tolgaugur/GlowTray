"""Render the README screenshots (docs/screenshot-*.png) from the real panel UI.

    python tools/screenshots.py

The panel (app/ui/index.html) is opened in headless Edge/Chromium with a stand-in for the Python API that returns
sample data, so the images show the current UI without any personal settings, bulb address or desktop. Needs Edge/Chromium
and Pillow (already a Glowtray dependency).
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
W, H, SCALE = 380, 640, 2  # the panel's client area in the app, rendered at 2x
SHOTS = {"light": ("screenshot-light.png", 515),
         "auto": ("screenshot-automation.png", 535),
         "stats": ("screenshot-stats.png", 510),
         "settings": ("screenshot-settings.png", 515)}

sys.path.insert(0, os.path.join(ROOT, "app"))
import config  # noqa: E402  (only DEFAULTS is used; nothing is read from or written to %APPDATA%)


def sample_data():
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg.update(theme="dark", first_run=False,
               device={"address": "00:00:00:00:00:00", "name": "Desk lamp"},
               location={"city": "Amsterdam", "lat": 52.3676, "lon": 4.9041})
    for rule in cfg["rules"]:
        rule["enabled"] = True
    cfg["rules"].append({
        "id": "r_game", "name": "Match screen while gaming", "enabled": False,
        "trigger": {"type": "process_start", "name": "game.exe"}, "conditions": [{"type": "dark", "value": True}],
        "action": {"type": "effect", "effect": "screen"}})
    return {
        "config": cfg,
        "state": {"connected": True, "busy": False, "error": None, "on": True, "bri": 78, "ct": 2700,
                  "rgb": "#ff7a2f", "mode": "xy", "has_color": True, "has_ct": True, "effect": None, "fading": False},
        "timer": None, "sun": {"sunrise": "08:02", "sunset": "18:51"}, "version": "1.2.0", "autostart": False,
        "frozen": True, "systemDark": True, "hotkeyErrors": [], "pinned": False,
        "flux": {"running": True, "kelvin": 3400},
        "activity": [{"time": "18:36", "source": "Turn on at sunset", "text": "Scene: Relax"},
                     {"time": "13:12", "source": "Turn off when PC locks", "text": "Turn light off"},
                     {"time": "07:30", "source": "Wake-up light", "text": "Fade to 100% over 15 min"}],
    }


def sample_stats():
    """Fixed illustrative stats; never access a user's tracking database."""
    from datetime import date, timedelta
    today = date(2026, 10, 12)
    series30 = [{"date": (today - timedelta(days=29 - i)).isoformat(),
                 "label": str((today - timedelta(days=29 - i)).day),
                 "on": 2500 + (i * 1217) % 9400} for i in range(30)]
    series7 = [{**v, "label": "MTWTFSS"[(today - timedelta(days=6 - i)).weekday()]}
               for i, v in enumerate(series30[-7:])]
    return {"since": "2026-09-01", "today": {"on": 5580, "avg_bri": 68},
            "week": {"on": 46100, "ons": 12},
            "month": {"on": 194000, "wh": 405, "avg_bri": 64, "ons": 46,
                      "hours": [0, 0, 0, 0, 0, 0, 0, 0, 8, 13, 20, 24, 14, 8, 14, 18, 26, 34, 42, 49, 40, 26, 12, 4]},
            "daily_avg": 7210, "series7": series7, "series30": series30,
            "top_scenes": [["Relax", 23], ["Read", 14], ["Sunset", 9]],
            "top_effects": [["screen", 17], ["candle", 6]], "price": 0, "watts": 9}


# Stands in for window.pywebview.api; ?tab=<name> opens that tab after init
MOCK = """<style>html,body{width:%dpx!important;height:%dpx!important}</style>
<script>
(() => {
  const INIT = %s;
  const STATS = %s;
  const fixed = { init: INIT, monitors: [], processes: [], stats: STATS };
  const api = new Proxy({}, { get: (_, k) => async () => k in fixed ? fixed[k] :
    k === "settings" ? { config: INIT.config, hotkeyErrors: [], autostart: false } :
    k === "sun" ? INIT.sun : k.startsWith("rule_") ? INIT.config.rules : { ok: true } });
  window.pywebview = { api };
  const tab = new URLSearchParams(location.search).get("tab");
  if (tab) addEventListener("load", () => setTimeout(() => setTab(tab), 300));
})();
</script>
"""


def find_browser():
    override = os.environ.get("GLOWTRAY_SCREENSHOT_BROWSER")
    if override:
        path = shutil.which(override)
        return path if path and os.path.isfile(path) else sys.exit(f"Browser not found: {override}")
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(path):
                return path
    for name in ("msedge", "chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("Microsoft Edge or Chromium was not found")


def main():
    browser = find_browser()
    with open(os.path.join(ROOT, "app", "ui", "index.html"), encoding="utf-8") as f:
        html = f.read()
    i = html.index("<script>")
    html = html[:i] + MOCK % (W, H, json.dumps(sample_data()), json.dumps(sample_stats())) + html[i:]

    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "panel.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        for tab, (name, crop_height) in SHOTS.items():
            raw = os.path.join(tmp, tab + ".png")
            # Headless Edge has a minimum window width above 380px, so render wider and crop
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--force-device-scale-factor={SCALE}", f"--window-size={W + 220},{H}",
                            "--virtual-time-budget=4000", f"--user-data-dir={os.path.join(tmp, 'profile')}",
                            f"--screenshot={raw}", f"{Path(page).as_uri()}?tab={tab}"],
                           check=True, capture_output=True, timeout=120)
            out = os.path.join(DOCS, name)
            with Image.open(raw) as im:
                im.crop((0, 0, W * SCALE, crop_height * SCALE)).save(out, optimize=True)
            print("Wrote", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
