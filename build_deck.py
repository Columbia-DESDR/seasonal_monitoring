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
# Layout. All inches. Measured off the hand-made bulletin, so the output
# lands in the same places as the version WFP already receives.
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

# The forecast slide carries two panels side by side rather than a 2x2, so
# they get more room. Proportions chosen so the map area comes out close to
# the shape of the PyCPT grid and little is wasted on white margins.
FC_PANEL_W, FC_PANEL_H = 4.30, 4.40
FC_PANEL_X = (1.26, 6.13)
FC_PANEL_Y = 1.55

# The charts are wider than they are tall, unlike the maps, so they get
# their own height. Placing them in the map slot would stretch them.
FC_CHART_H = 3.20
FC_CHART_Y = 1.70


# ----------------------------------------------------------------------
# Stage 1: compose each map into a titled panel with a colour bar
# ----------------------------------------------------------------------

def compose_panel(raw_path, title, palette, vmin, vmax, units, out_path,
                  dpi=300, width=PANEL_W, height=PANEL_H):
    """Stack a title, the map, and a colour bar into one panel image.

    The maps that come back from Earth Engine are bare. In the existing
    bulletin the titles and colour bars are part of the picture rather than
    separate text boxes, so they are drawn here.

    width and height are in inches and the panel is placed on the slide at
    exactly that size, so the text stays the same physical size whichever
    panel it lands in.
    """
    image = plt.imread(raw_path)

    fig = plt.figure(figsize=(width, height), dpi=dpi)
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


def computed_forecast_slide(prs, forecast, running_head, page_number,
                            raw_dir, panel_dir):
    """The forecast slide built from forecast.py's output.

    Two panels: the drought probability, and the forecast's departure from
    normal that explains it. Composed the same way as every other panel, so
    the titles and colour bars match the rest of the deck.
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_chrome(slide, running_head, page_number)

    add_text(slide, forecast.get("heading", "Seasonal Forecast"),
             TITLE["left"], TITLE["top"], TITLE["width"], TITLE["height"],
             size=TITLE["size"], bold=True)

    subtitle = (f"{forecast['season']} | "
                f"hindcast {forecast['hindcast_start']}-{forecast['hindcast_end']} | "
                f"drought threshold at the "
                f"{forecast['threshold_percentile']}th percentile of "
                f"{forecast.get('threshold_source', 'the hindcast')}")
    add_text(slide, subtitle,
             SUBTITLE["left"], SUBTITLE["top"],
             SUBTITLE["width"], SUBTITLE["height"],
             size=SUBTITLE["size"], color=MUTED)

    for i, panel in enumerate(forecast["panels"][:2]):
        name = panel["raw_image"]
        compose_panel(
            raw_dir / name,
            panel["title"], panel["palette"],
            panel["min"], panel["max"], panel["units"],
            panel_dir / name,
            width=FC_PANEL_W, height=FC_PANEL_H,
        )
        slide.shapes.add_picture(str(panel_dir / name),
                                 Inches(FC_PANEL_X[i]), Inches(FC_PANEL_Y),
                                 Inches(FC_PANEL_W), Inches(FC_PANEL_H))

    if forecast.get("note"):
        add_text(slide, forecast["note"],
                 0.60, FC_PANEL_Y + FC_PANEL_H + 0.22, 10.49, 0.30,
                 size=8, color=MUTED)
    return True


def forecast_chart_slide(prs, forecast, running_head, page_number, raw_dir,
                         charts=None, label=None, has_conditional=None):
    """How the season is tracking, and the spread of trigger
    probabilities, for one area.

    Separate from the maps because these answer a different question. The
    maps say where; these say whether, and they are read as numbers rather
    than as geography. Returns False when forecast.py had nothing to draw,
    which is the normal state before CHIRPS has been pulled.

    Pass charts and label to draw one admin unit instead of the whole
    country. The slide is otherwise identical, which is the point: a
    governorate should be readable the same way as the country.
    """
    if charts is None:
        charts = forecast.get("chart_panels") or []
        has_conditional = forecast.get("has_conditional")
    if not charts:
        return False

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_chrome(slide, running_head, page_number)

    heading = forecast.get("heading", "Seasonal Forecast")
    add_text(slide, f"{label}: season to date" if label
             else f"{heading}: season to date",
             TITLE["left"], TITLE["top"], TITLE["width"], TITLE["height"],
             size=TITLE["size"], bold=True)
    add_text(slide,
             f"{forecast['season']} | "
             f"{'average over ' + label if label else 'country average'} | "
             f"threshold at the {forecast['threshold_percentile']}th "
             f"percentile of {forecast.get('threshold_source', '')}",
             SUBTITLE["left"], SUBTITLE["top"],
             SUBTITLE["width"], SUBTITLE["height"],
             size=SUBTITLE["size"], color=MUTED)

    # Already titled and labelled by charts.py, so these are placed as they
    # are rather than run through compose_panel.
    # Outside the season there is only one chart, and leaving it in the
    # left-hand slot makes the slide look broken rather than sparse.
    if len(charts) == 1:
        positions = [(SLIDE_W_EMU / 914400 - FC_PANEL_W) / 2]
    else:
        positions = list(FC_PANEL_X)

    for panel, left in zip(charts[:2], positions):
        slide.shapes.add_picture(str(raw_dir / panel["image"]),
                                 Inches(left), Inches(FC_CHART_Y),
                                 Inches(FC_PANEL_W), Inches(FC_CHART_H))

    if has_conditional:
        caption = ("The conditional line is the only one here that settles "
                   "as the season runs. It finds the past seasons that had "
                   "about as much rain by this point as this one, and "
                   "counts how many of them finished below the threshold. "
                   "Early on almost every year qualifies and it sits near "
                   "the base rate; late on only a few do, and it sharpens.")
    else:
        # Saying why a series is missing is worth more than leaving a
        # reader to wonder whether it was forgotten.
        caption = ("A third line, conditional on the rain already fallen, "
                   "appears once the season is under way. Observed rainfall "
                   "runs about three weeks behind, so it is absent until "
                   "the record catches up with the season's start.")
    add_text(slide, caption,
             0.60, FC_CHART_Y + FC_CHART_H + 0.35, 10.49, 0.50,
             size=8, color=MUTED)
    return True


def screenshot_forecast_slide(prs, config, running_head, page_number,
                              base_dir):
    """Fallback. Screenshots from the PyCPT maproom, placed as pictures.

    This is what the hand-made bulletin did. Used only when no computed
    forecast is available.
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


def method_slide(prs, config, manifest, running_head, page_number,
                 forecast=None):
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

    # The forecast goes in the right column. It has a different provenance
    # from the monitoring layers, and saying so matters: these are model
    # output rather than observations, and the threshold comes from the
    # model's own history rather than from a rain gauge.
    if forecast:
        add_text(slide, "Seasonal forecast", 5.90, 1.40, 4.80, 0.40,
                 size=10, bold=True)
        detail = (f"{forecast.get('source_detail', '')}. "
                  f"Season {forecast['season']}. "
                  f"Hindcast {forecast['hindcast_start']}-"
                  f"{forecast['hindcast_end']}.")
        add_text(slide, detail, 6.05, 1.68, 4.65, 0.70, size=8.5)
        add_text(slide,
                 "Drought probability is the chance the forecast season "
                 f"falls below the {forecast['threshold_percentile']}th "
                 "percentile of the hindcast for that pixel, from a normal "
                 "distribution centred on the forecast mean with the "
                 "forecast prediction error variance as its spread. The "
                 f"threshold makes {forecast['threshold_percentile'] / 100:.2f} "
                 "the long-run base rate, so values above that are elevated "
                 f"risk. Threshold taken from {forecast.get('threshold_source', '')}.",
                 6.05, 2.45, 4.65, 1.10, size=8)

        if not forecast.get("calibrated", True):
            add_text(slide, "Known limitation", 5.90, 3.70, 4.80, 0.40,
                     size=10, bold=True)
            add_text(slide,
                     "The prediction error variance is measured against "
                     "observed rainfall, so the threshold should be too. "
                     "Taken from the hindcast instead, it sits on the "
                     "model's own predictions, whose year-to-year spread is "
                     "narrower than reality's, so the percentile is not "
                     "extreme enough. A forecast carrying no information "
                     f"returns {forecast.get('null_probability')} here "
                     f"rather than "
                     f"{forecast['threshold_percentile'] / 100:.2f}, so "
                     "every probability on the map reads high. The fix is "
                     "an observed rainfall record on the forecast grid.",
                     6.05, 3.98, 4.65, 1.80, size=8, color=MUTED)

    add_text(slide, "Method", 0.60, 4.25, 4.80, 0.40, size=10, bold=True)
    add_text(slide, config["method_text"], 0.75, 4.55, 4.65, 1.90, size=8)

    add_text(slide, "Disclaimer", 0.60, 6.60, 4.80, 0.40, size=10, bold=True)
    add_text(slide, config["disclaimer_text"], 0.75, 6.90, 4.65, 1.20,
             size=7.5, color=MUTED)
    return slide


# ----------------------------------------------------------------------

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def show_regions(out_dir):
    """Print the governorates that --regions will accept.

    Read out of observed.json rather than asked of Earth Engine, so it
    is instant and needs no credentials. They are exactly the units
    observed.py pulled rainfall for, which is exactly the set --regions
    can draw.
    """
    path = out_dir / "observed.json"
    if not path.exists():
        raise SystemExit(f"No {path.name} yet, so there is nothing to "
                         f"list. Run observed.py first.")

    regions = json.loads(path.read_text()).get("regions") or {}
    if not regions:
        raise SystemExit(
            "observed.json holds no admin units. Set regional_data: true "
            "in config.yaml\nand run observed.py again."
        )

    rows = sorted((v.get("name", key), key) for key, v in regions.items())
    width = max(len(name) for name, _ in rows)
    print(f"{len(rows)} regions. Pass any of these to --regions, by name "
          f"or by id:\n")
    for name, uid in rows:
        print(f"  {name:<{width}}   {uid}")
    print(f'\n  python build_deck.py --regions "{rows[0][0]}"')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", action="store_true",
                        help="also export a PDF (needs LibreOffice)")
    parser.add_argument(
        "--regions", nargs="+", metavar="ID", default=[],
        help="admin units to chart, by name or by id. Given, the "
             "season-to-date slide is replaced by one per unit. The "
             "maps stay national.")
    parser.add_argument(
        "--list-regions", action="store_true",
        help="print the units --regions accepts, then stop")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    with open(base_dir / "config.yaml") as f:
        config = yaml.safe_load(f)

    out_dir = base_dir / config["output"]["dir"]
    if args.list_regions:
        show_regions(out_dir)
        return

    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"No manifest at {manifest_path}. Run fetch.py first.")

    manifest = json.loads(manifest_path.read_text())
    raw_dir = out_dir / "raw"
    panel_dir = out_dir / "panels"
    panel_dir.mkdir(parents=True, exist_ok=True)

    # Written by forecast.py when forecast files are configured. Absent is
    # normal and just means no forecast slide.
    # The maps are national whatever happens. Only the two charts
    # change with --regions, and forecast.py is what draws them, so it
    # runs again here every time rather than the deck trying to work out
    # whether what is on disk still answers the question. Costs a few
    # seconds and means the flag, and its absence, always do what they
    # say.
    forecast_path = out_dir / "forecast.json"
    if forecast_path.exists():
        import forecast as forecast_step
        wanted = list(args.regions)
        print(f"Charts for {len(wanted)} region"
              f"{'' if len(wanted) == 1 else 's'} ..." if wanted
              else "Charts for the whole country ...")
        forecast_step.main((["--regions"] + wanted) if wanted else [])
        print()

    forecast = (json.loads(forecast_path.read_text())
                if forecast_path.exists() else None)

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
    if forecast:
        contents.append(forecast.get("heading", "Seasonal Forecast"))
        groups = forecast.get("regional_charts") or []
        if groups:
            contents += [f"{g['label']}: season to date" for g in groups]
        elif forecast.get("chart_panels"):
            contents.append(f"{forecast.get('heading', 'Seasonal Forecast')}"
                            f": season to date")
    elif (config.get("forecasts") or {}).get("images"):
        contents.append("Forecasts")
    contents += [s["label"] for s in manifest["slides"]]

    title_slide(prs, manifest, month_label, contents)

    page = 2
    if forecast:
        computed_forecast_slide(prs, forecast, running_head, page,
                                raw_dir, panel_dir)
        page += 1
        groups = forecast.get("regional_charts") or []
        if groups:
            # One slide per unit, in the order asked for, each laid out
            # exactly like the national one so they can be read the same
            # way and compared with each other.
            for group in groups:
                if forecast_chart_slide(
                        prs, forecast, running_head, page, raw_dir,
                        charts=group.get("panels"),
                        label=group.get("label"),
                        has_conditional=group.get("has_conditional")):
                    page += 1
        elif forecast_chart_slide(prs, forecast, running_head, page, raw_dir):
            page += 1
    elif screenshot_forecast_slide(prs, config, running_head, page, base_dir):
        page += 1

    for slide_data in manifest["slides"]:
        variable_slide(prs, slide_data, running_head, page, panel_dir)
        page += 1

    method_slide(prs, config, manifest, running_head, page, forecast)

    slug = re.sub(r"[^a-z0-9]+", "_", manifest["region"].lower()).strip("_")
    pptx_path = out_dir / f"{slug}_bulletin_{manifest['year']}_{manifest['month']:02d}.pptx"
    prs.save(pptx_path)
    print(f"Wrote {pptx_path}")

    if args.pdf:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            print("LibreOffice not found, skipping the PDF.")
            return
        # The deck is already written and saved by this point, so a failed
        # conversion costs the PDF and nothing else. Crashing here would
        # throw away a good run over an optional extra.
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", str(out_dir), str(pptx_path)],
                           check=True, capture_output=True, timeout=300)
            print(f"Wrote {pptx_path.with_suffix('.pdf')}")
        except subprocess.TimeoutExpired:
            print("LibreOffice took too long, so no PDF. The deck is fine.")
        except subprocess.CalledProcessError as error:
            print(f"LibreOffice failed, so no PDF. The deck is fine.")
            if error.returncode == -9:
                # Killed outright rather than exiting with an error. On
                # macOS that is Gatekeeper stopping an app that has not
                # been opened once by hand since it was installed.
                print("  It was killed rather than erroring, which on macOS "
                      "usually means\n  the app has not been approved yet. "
                      "Either open LibreOffice once\n  from Applications, or "
                      "run:\n"
                      "    xattr -dr com.apple.quarantine "
                      "/Applications/LibreOffice.app")
            else:
                message = (error.stderr or b"").decode(errors="replace").strip()
                if message:
                    print(f"  {message.splitlines()[0]}")


if __name__ == "__main__":
    main()