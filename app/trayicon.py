"""Tray icon: a bulb drawn on the fly in the light's current color."""
from PIL import Image, ImageDraw

import light as L
import winutil

LIGHT_TASKBAR = winutil.taskbar_light()


def draw_bulb(color=None, on=False, connected=True, size=64):
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    line = ((30, 30, 30, 255) if LIGHT_TASKBAR else (240, 240, 240, 255)) if connected else (150, 150, 150, 255)
    fill = tuple(color) + (255,) if on and color else (0, 0, 0, 0)
    w = 20
    # glass + neck in one piece
    d.ellipse((34, 6, 222, 194), fill=fill)
    d.polygon([(78, 160), (178, 160), (166, 200), (90, 200)], fill=fill)
    d.arc((34, 6, 222, 194), start=128, end=412, fill=line, width=w)
    d.line([(76, 172), (92, 202)], fill=line, width=w)
    d.line([(180, 172), (164, 202)], fill=line, width=w)
    # base
    d.rounded_rectangle((86, 204, 170, 228), radius=10, fill=line)
    d.rounded_rectangle((100, 234, 156, 254), radius=10, fill=line)
    if on:
        # highlight
        d.arc((70, 42, 150, 122), start=180, end=260, fill=(255, 255, 255, 200), width=14)
    return img.resize((size, size), Image.LANCZOS)


def icon_for_state(st):
    color = None
    if st.get("on"):
        if st.get("mode") == "xy" and st.get("rgb"):
            color = L.hex_to_rgb(st["rgb"])
        elif st.get("ct"):
            color = L.kelvin_to_rgb(st["ct"])
        else:
            color = (255, 214, 140)
    return draw_bulb(color, bool(st.get("on")), True)


def save_ico(path):
    img = draw_bulb((255, 196, 90), True, True, 256)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
