"""
Turn the fetched maps into the bulletin deck.

Two stages, both fast, so re-running this while you fiddle with layout costs
nothing:

  1. compose  - each raw map gets its title and colour bar, via matplotlib
  2. assemble - the panels are placed into slides, via python-pptx

Reads output/manifest.json. No Earth Engine here.

    python build_deck.py            -> output/<region>_bulletin_<month>.pptx
    python build_deck.py --pdf      -> also exports a PDF, needs LibreOffice
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml
from matplotlib.colorbar import ColorbarBase
from matplotlib.colors import LinearSegmentedColormap, Normalize
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt


# ----------------------------------------------------------------------
# Layout. All inches. Lifted from the bulletin deck Nitin built, so the
# output lands in the same places as the version WFP already receives.
# ----------------------------------------------------------------------

SLIDE_W_EMU = 10688638      # 11.69 in, A4 landscape
SLIDE_H_EMU = 7562850       # 8.27 in

FONT = "Calibri"
INK = "222222"
MUTED = "666666"

HEADER = dict(left=6.49, top=0.28, width=4.80, height=0.40, size=7.5)
PAGENO = dict(left=10.99, top=7.77, width=0.30, height=0.40, size=7.5)
TITLE = dict(left=0.50, top=0.55, width=7.00, height=0.45, size=18)
SUBTITLE = dict(left=0.50, top=1.02, width=10.69, height=0.40, size=9)

PANEL_W, PANEL_H = 3.45, 2.56
PANEL_X = (1.45, 6.79)
PANEL_Y = (1.45, 4.55)


# ----------------------------------------------------------------------
# Stage 1: compose each map into a titled panel with a colour bar
# ----------------------------------------------------------------------

def compose_panel(raw_path, title, palette, vmin, vmax, units, out_path,
                  dpi=300):
    """Stack a title, the map, and a colour bar into one panel image.

    The maps that come back from Earth Engine are bare. In the existing
    bulletin the titles and colour bars are part of the picture rather than
    separate text boxes, so they are drawn here.
    """
    image = plt.imread(raw_path)

    fig = plt.figure(figsize=(PANEL_W, PANEL_H), dpi=dpi)
    fig.patch.set_facecolor("white")

    fig.text(0.5, 0.945, title, ha="center", va="center",
             fontsize=7.5, fontweight="bold", color="#" + INK)

    ax = fig.add_axes([0.03, 0.19, 0.94, 0.70])
    ax.imshow(image)
    ax.axis("off")

    cax = fig.add_axes([0.16, 0.105, 0.68, 0.032])
    cmap = LinearSegmentedColormap.from_list(
        "panel", ["#" + c for c in palette])
    bar = ColorbarBase(cax, cmap=cmap, norm=Normalize(vmin, vmax),
                       orientation="horizontal")
    bar.set_ticks([vmin, (vmin + vmax) / 2, vmax])
    bar.ax.tick_params(labelsize=4.5, length=1.5, pad=1, colors="#444444")
    bar.outline.set_linewidth(0.4)
    bar.outline.set_edgecolor("#999999")

    fig.text(0.5, 0.022, units, ha="center", fontsize=4.5, color="#" + MUTED)

    fig.savefig(out_path, dpi=dpi, facecolor="white")
    plt.close(fig)


# ----------------------------------------------------------------------
# Stage 2: assemble the slides
# ----------------------------------------------------------------------

def add_text(slide, text, left, top, width, height, size,
             bold=False, color=INK, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(left), Inches(top),
                                   Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0

    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    run = paragraph.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = FONT
    run.font.color.rgb = RGBColor.from_string(color)
    return box


def add_chrome(slide, running_head, page_number):
    """The header and page number that repeat on every slide after the first."""
    add_text(slide, running_head, size=HEADER["size"], color=MUTED,
             align=PP_ALIGN.RIGHT,
             left=HEADER["left"], top=HEADER["top"],
             width=HEADER["width"], height=HEADER["height"])
    add_text(slide, str(page_number), size=PAGENO["size"], color=MUTED,
             left=PAGENO["left"], top=PAGENO["top"],
             width=PAGENO["width"], height=PAGENO["height"])


def title_slide(prs, manifest, month_label, contents):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_text(slide, manifest["title"], 0.70, 1.90, 10.29, 0.80,
             size=30, bold=True)
    add_text(slide, month_label, 0.70, 2.80, 10.29, 0.40,
             size=17, color=MUTED)
    add_text(slide, "Contents", 0.70, 3.90, 4.00, 0.40, size=11, bold=True)
    for i, entry in enumerate(contents):
        add_text(slide, f"{i + 1}. {entry}", 0.95, 4.35 + i * 0.33,
                 5.00, 0.17, size=10)
    return slide


def forecast_slide(prs, config, running_head, page_number, base_dir):
    """Optional. Screenshots from the PyCPT maproom, which is not Earth Engine.

    Skipped entirely when no images are listed in the config.
    """
    forecasts = config.get("forecasts") or {}
    images = forecasts.get("images") or []
    if not images:
        return False

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_chrome(slide, running_head, page_number)
    add_text(slide, forecasts.get("heading", "Forecasts"),
             TITLE["left"], TITLE["top"], TITLE["width"], TITLE["height"],
             size=TITLE["size"], bold=True)
    add_text(slide, forecasts.get("note", ""),
             0.50, 1.02, 10.69, 0.15, size=9, color=MUTED)

    folder = base_dir / forecasts.get("dir", "forecasts")
    positions = [(0.82, 2.05, 4.55, 1.84), (5.79, 2.05, 4.55, 1.84),
                 (2.80, 4.73, 4.55, 1.84)]
    for entry, (x, y, w, h) in zip(images, positions):
        path = folder / entry["file"]
        if not path.exists():
            print(f"  forecast image missing, skipping: {path}")
            continue
        slide.shapes.add_picture(str(path), Inches(x), Inches(y),
                                 Inches(w), Inches(h))
        add_text(slide, entry.get("caption", ""), x, y + h + 0.07,
                 w, 0.11, size=6.5)
    return True


def variable_slide(prs, slide_data, running_head, page_number, panel_dir):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_chrome(slide, running_head, page_number)

    add_text(slide, slide_data["label"],
             TITLE["left"], TITLE["top"], TITLE["width"], TITLE["height"],
             size=TITLE["size"], bold=True)

    subtitle = (f"{slide_data['source_label']} | "
                f"climatology {slide_data['clim_start']}-{slide_data['clim_end']} | "
                f"data through {slide_data['data_through']}")
    add_text(slide, subtitle,
             SUBTITLE["left"], SUBTITLE["top"],
             SUBTITLE["width"], SUBTITLE["height"],
             size=SUBTITLE["size"], color=MUTED)

    # Top row is the standardized anomaly, bottom row the second view.
    # Columns are the 1-month and 3-month windows.
    order = {("standardized", "1month"): (0, 0),
             ("standardized", "3month"): (0, 1)}
    for panel in slide_data["panels"]:
        if panel["band"] == "standardized":
            row, col = order[(panel["band"], panel["window"])]
        else:
            row, col = 1, (0 if panel["window"] == "1month" else 1)

        path = panel_dir / panel["panel_image"]
        slide.shapes.add_picture(str(path),
                                 Inches(PANEL_X[col]), Inches(PANEL_Y[row]),
                                 Inches(PANEL_W), Inches(PANEL_H))
    return slide


def method_slide(prs, config, manifest, running_head, page_number):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_chrome(slide, running_head, page_number)
    add_text(slide, "Data sources and method",
             TITLE["left"], TITLE["top"], TITLE["width"], TITLE["height"],
             size=TITLE["size"], bold=True)

    y = 1.40
    for slide_data in manifest["slides"]:
        add_text(slide, f"Standardized {slide_data['label'].lower()}",
                 0.60, y, 4.80, 0.40, size=10, bold=True)
        detail = (f"{slide_data['source_detail']}. "
                  f"Climatology {slide_data['clim_start']}-{slide_data['clim_end']}. "
                  f"Data through {slide_data['data_through']}.")
        add_text(slide, detail, 0.75, y + 0.28, 4.65, 0.60, size=8.5)
        y += 0.90

    add_text(slide, "Method", 0.60, 4.25, 4.80, 0.40, size=10, bold=True)
    add_text(slide, config["method_text"], 0.75, 4.55, 4.65, 1.90, size=8)

    add_text(slide, "Disclaimer", 0.60, 6.60, 4.80, 0.40, size=10, bold=True)
    add_text(slide, config["disclaimer_text"], 0.75, 6.90, 4.65, 1.20,
             size=7.5, color=MUTED)
    return slide


# ----------------------------------------------------------------------

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", action="store_true",
                        help="also export a PDF (needs LibreOffice)")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    with open(base_dir / "config.yaml") as f:
        config = yaml.safe_load(f)

    out_dir = base_dir / config["output"]["dir"]
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"No manifest at {manifest_path}. Run fetch.py first.")

    manifest = json.loads(manifest_path.read_text())
    raw_dir = out_dir / "raw"
    panel_dir = out_dir / "panels"
    panel_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1
    print("Composing panels ...")
    for slide_data in manifest["slides"]:
        for panel in slide_data["panels"]:
            name = panel["raw_image"]
            compose_panel(
                raw_dir / name,
                panel["title"], panel["palette"],
                panel["min"], panel["max"], panel["units"],
                panel_dir / name,
            )
            panel["panel_image"] = name
    print(f"  {sum(len(s['panels']) for s in manifest['slides'])} panels")

    # Stage 2
    print("Assembling slides ...")
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W_EMU)
    prs.slide_height = Emu(SLIDE_H_EMU)

    month_label = f"{MONTHS[manifest['month'] - 1]} {manifest['year']}"
    running_head = f"{manifest['title']} | {month_label}"

    contents = []
    if (config.get("forecasts") or {}).get("images"):
        contents.append("Forecasts")
    contents += [s["label"] for s in manifest["slides"]]

    title_slide(prs, manifest, month_label, contents)

    page = 2
    if forecast_slide(prs, config, running_head, page, base_dir):
        page += 1

    for slide_data in manifest["slides"]:
        variable_slide(prs, slide_data, running_head, page, panel_dir)
        page += 1

    method_slide(prs, config, manifest, running_head, page)

    slug = re.sub(r"[^a-z0-9]+", "_", manifest["region"].lower()).strip("_")
    pptx_path = out_dir / f"{slug}_bulletin_{manifest['year']}_{manifest['month']:02d}.pptx"
    prs.save(pptx_path)
    print(f"Wrote {pptx_path}")

    if args.pdf:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            print("LibreOffice not found, skipping the PDF.")
            return
        subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                        "--outdir", str(out_dir), str(pptx_path)],
                       check=True, capture_output=True)
        print(f"Wrote {pptx_path.with_suffix('.pdf')}")


if __name__ == "__main__":
    main()
