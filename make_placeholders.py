"""
Generate fake maps and a manifest, so you can work on the deck before Earth
Engine authentication is sorted out.

    python make_placeholders.py
    python build_deck.py

Nothing here touches Earth Engine. Clear the output folder and run fetch.py
once you have real data.
"""

import json
import math
from datetime import date
from pathlib import Path

import yaml
from PIL import Image

from bands import BAND_SPECS, rows_for, panel_title


def hex_to_rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def fake_map(palette, width=900, height=560):
    """A smooth blobby field standing in for a real anomaly map."""
    colors = [hex_to_rgb(c) for c in palette]
    image = Image.new("RGB", (width, height), (255, 255, 255))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            t = (math.sin(x / 110) * math.cos(y / 80) + 1) / 2
            position = t * (len(colors) - 1)
            i = min(int(position), len(colors) - 2)
            f = position - i
            pixels[x, y] = tuple(
                int(colors[i][k] * (1 - f) + colors[i + 1][k] * f)
                for k in range(3)
            )
    return image


def main():
    base_dir = Path(__file__).parent
    with open(base_dir / "config.yaml") as f:
        config = yaml.safe_load(f)

    out_dir = base_dir / config["output"]["dir"]
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    region = config["region"]
    bulletin = config["bulletin"]
    prefix = region["filter_value"].lower()

    # The real run asks Earth Engine which month it can cover. With no
    # Earth Engine here, fall back to last month.
    year, month = bulletin.get("year"), bulletin.get("month")
    if not (year and month):
        today = date.today()
        month, year = today.month - 1, today.year
        if month < 1:
            month, year = 12, year - 1

    slides = []
    count = 0
    for layer in config["layers"]:
        panels = []
        for band in rows_for(layer):
            for window in config["windows"]:
                spec = BAND_SPECS[band]
                filename = f"{prefix}_{layer['key']}_{band}_{window['key']}.png"
                fake_map(layer["palette"]).save(raw_dir / filename)
                count += 1
                panels.append({
                    "band": band,
                    "window": window["key"],
                    "raw_image": filename,
                    "title": panel_title(band, layer["label"],
                                         window["label"]),
                    "units": spec["units"],
                    "min": spec["min"],
                    "max": spec["max"],
                    "palette": layer["palette"],
                })

        slides.append({
            "key": layer["key"],
            "label": layer["label"],
            "source_label": layer["source_label"],
            "source_detail": layer["source_detail"],
            "clim_start": layer["clim_start"],
            "clim_end": layer["clim_end"],
            "data_through": "PLACEHOLDER",
            "panels": panels,
        })

    manifest = {
        "region": region["name"],
        "title": bulletin["title"] + " (PLACEHOLDER DATA)",
        "year": year,
        "month": month,
        "generated": date.today().isoformat(),
        "slides": slides,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {count} placeholder maps to {raw_dir}/")
    print("These are fake. Run build_deck.py to check the layout.")


if __name__ == "__main__":
    main()
