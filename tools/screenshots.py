"""Render the README screenshots (docs/screenshot-*.png) from the real panel UI.

    python tools/screenshots.py

The panel (app/ui/index.html) is opened in headless Microsoft Edge with a stand-in for the Python API that returns
sample data, so the images show the current UI without any personal settings, bulb address or desktop. Needs Edge
and Pillow (already a Glowtray dependency).
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
W, H, SCALE = 380, 640, 2  # the panel's client area in the app, rendered at 2x
SHOTS = {"light": "screenshot-light.png", "auto": "screenshot-automation.png"}

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


# Stands in for window.pywebview.api; ?tab=<name> opens that tab after init
MOCK = """<style>html,body{width:%dpx!important;height:%dpx!important}</style>
<script>
(() => {
  const INIT = %s;
  const fixed = { init: INIT, monitors: [], processes: [], stats: null };
  const api = new Proxy({}, { get: (_, k) => async () => k in fixed ? fixed[k] :
    k === "settings" ? { config: INIT.config, hotkeyErrors: [], autostart: false } :
    k === "sun" ? INIT.sun : k.startsWith("rule_") ? INIT.config.rules : { ok: true } });
  window.pywebview = { api };
  const tab = new URLSearchParams(location.search).get("tab");
  if (tab) addEventListener("load", () => setTimeout(() => setTab(tab), 300));
})();
</script>
"""


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(path):
                return path
    return shutil.which("msedge") or sys.exit("Microsoft Edge was not found")


def main():
    edge = find_edge()
    with open(os.path.join(ROOT, "app", "ui", "index.html"), encoding="utf-8") as f:
        html = f.read()
    i = html.index("<script>")
    html = html[:i] + MOCK % (W, H, json.dumps(sample_data())) + html[i:]

    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "panel.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        for tab, name in SHOTS.items():
            raw = os.path.join(tmp, tab + ".png")
            # Headless Edge has a minimum window width above 380px, so render wider and crop
            subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--force-device-scale-factor={SCALE}", f"--window-size={W + 220},{H}",
                            "--virtual-time-budget=4000", f"--user-data-dir={os.path.join(tmp, 'profile')}",
                            f"--screenshot={raw}", f"file:///{page.replace(os.sep, '/')}?tab={tab}"],
                           check=True, capture_output=True, timeout=120)
            out = os.path.join(DOCS, name)
            with Image.open(raw) as im:
                im.crop((0, 0, W * SCALE, H * SCALE)).save(out, optimize=True)
            print("Wrote", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
