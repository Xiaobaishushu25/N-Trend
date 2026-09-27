"""
Generate high-resolution icons for N-Trend (desktop + Android adaptive icons).
"""

from pathlib import Path
import json
import subprocess
import shutil
from PIL import Image, ImageDraw
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT / "src-tauri" / "icons"
RES_DIR = ROOT / "src-tauri" / "gen" / "android" / "app" / "src" / "main" / "res"

W, H = 1024, 1024

# 1. Background gradient: deep navy blue #16245e -> #1f338b
bg_arr = np.zeros((H, W, 4), dtype=np.uint8)
bg_arr[:, :, 3] = 255
for y in range(H):
    t = y / (H - 1)
    r = int(22 * (1 - t) + 31 * t)
    g = int(36 * (1 - t) + 51 * t)
    b = int(94 * (1 - t) + 139 * t)
    bg_arr[y, :, 0] = r
    bg_arr[y, :, 1] = g
    bg_arr[y, :, 2] = b

bg_img = Image.fromarray(bg_arr, "RGBA")
ICONS_DIR.mkdir(parents=True, exist_ok=True)
bg_img.save(ICONS_DIR / "icon-bg.png")

# 2. Polygon normalized 0..1 (92x92 in 128x128 box)
poly_norm = [
    (0.0, 0.0),
    (0.0, 1.0),
    (30.0 / 92.0, 1.0),
    (76.0 / 92.0, 22.0 / 92.0),
    (76.0 / 92.0, 1.0),
    (1.0, 1.0),
    (1.0, 0.0),
    (62.0 / 92.0, 0.0),
    (16.0 / 92.0, 70.0 / 92.0),
    (16.0 / 92.0, 0.0),
]


def make_fg(box_size):
    fg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fg)
    x0 = (W - box_size) / 2
    y0 = (H - box_size) / 2
    poly = [(x0 + px * box_size, y0 + py * box_size) for px, py in poly_norm]
    draw.polygon(poly, fill=(255, 210, 63, 255))
    return fg


# Desktop icon (box_size = 736, matches original 92/128 ratio)
fg_desktop = make_fg(736)
desktop_icon = Image.alpha_composite(bg_img, fg_desktop)
desktop_icon.save(ICONS_DIR / "icon.png")

# Android adaptive foreground (box_size = 420, perfectly fits 66% circular safe zone)
fg_android = make_fg(420)
fg_android.save(ICONS_DIR / "icon-fg.png")

# Manifest
manifest = {
    "default": "icon.png",
    "bg_color": "#16245e",
    "android_bg": "icon-bg.png",
    "android_fg": "icon-fg.png",
    "android_fg_scale": 60,
}
with open(ICONS_DIR / "icon-manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)

print("Base assets created. Running Tauri CLI icon generator...")
cmd = ["npm", "run", "tauri", "--", "icon", "src-tauri/icons/icon-manifest.json"]
subprocess.run(cmd, cwd=ROOT, shell=True, check=True)

# Ensure values/ic_launcher_background.xml has #16245e
color_xml = """<?xml version="1.0" encoding="utf-8"?>
<resources>
  <color name="ic_launcher_background">#16245e</color>
</resources>
"""
for p in [
    ICONS_DIR / "android" / "values" / "ic_launcher_background.xml",
    RES_DIR / "values" / "ic_launcher_background.xml",
]:
    if p.parent.exists():
        p.write_text(color_xml, encoding="utf-8")

# Sync to res folder if not already populated
if (ICONS_DIR / "android").exists():
    RES_DIR.mkdir(parents=True, exist_ok=True)
    for item in (ICONS_DIR / "android").iterdir():
        target = RES_DIR / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

print("All Android & desktop icon assets generated and synced successfully!")
