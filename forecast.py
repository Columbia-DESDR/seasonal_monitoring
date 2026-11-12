"""
Turn a PyCPT seasonal forecast into the drought-probability map.

This is the Python version of what yemen_seasonal_forecast_monitoring does in
the Code Editor, with one difference: the forecast is read from GeoTIFFs on
disk rather than from an Earth Engine asset, because nothing uploads PyCPT
output to Earth Engine yet. When that pipeline exists, only load_raster()
needs to change.

The question the map answers is not "how much rain will fall" but "what is
the chance this season comes in dry enough to count as a drought". That needs
three things:

  threshold  the 20th percentile of the hindcast for each pixel, so a
             drought means dry relative to what this place normally gets,
             rather than below some fixed number of millimetres
  forecast   PyCPT's predicted seasonal total (mu)
  spread     PyCPT's prediction error variance (var), which is what makes a
             probability possible at all. A single predicted number can only
             be above or below the threshold. A distribution can be asked
             how much of itself falls below it.

Then:

    z    = (threshold - mu) / sqrt(var)
    prob = normal CDF of z

A positive z means the threshold sits above the middle of the forecast, so
the forecast is dry and the probability is high. Because the threshold is the
20th percentile, 0.20 is the no-information baseline: that is roughly what
the probability comes out as when the forecast says nothing useful. Anything
much above 0.20 is elevated risk.

    python forecast.py

Reads the files named in config.yaml, writes maps into output/raw/ and
output/forecast.json. build_deck.py picks that up. Skips quietly and exits 0
when the files are not configured, so the scheduled run still produces a
bulletin without a forecast slide.
"""

import argparse
import json
import math
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib import patheffects
from matplotlib.colors import LinearSegmentedColormap
from scipy import ndimage

import yaml
from scipy.stats import norm

from charts import (CLIM, CONDTL, FCAST_PROB,
                    probability_distributions, season_to_date)


# How much finer to draw the map than the model grid. Presentation only:
# it changes nothing about the numbers, it just stops a 0.25-degree grid
# from reading as a mosaic next to the far finer CHIRPS maps.
UPSAMPLE = 8


# The baseline probability implied by the threshold. A 20th-percentile
# threshold means a year we know nothing about has a 20% chance of falling
# below it, so this is what the map is read against.
def baseline(percentile):
    return percentile / 100.0


def centre_registered(src):
    """Bounds this file should have had, if it was written half a cell off.

    PyCPT's X and Y coordinates are pixel centres. A converter that
    passes them straight to from_bounds() treats them as the outer edge
    of the raster instead, which shrinks every cell and shifts the whole
    grid half a cell. For Yemen that is about 50 km, and 1 degree cells
    come out as 0.9677 by 0.9375.

    The tell is a cell size that is not round as it stands but is round
    under the correction: 31 cells spanning 30 degrees is 0.9677 each,
    and spanning 31 would be exactly 1. Nothing else lands on that
    pattern by accident, which is what makes it safe to fix rather than
    only warn about. Returns None when the file is fine.
    """
    if src.width < 2 or src.height < 2:
        return None
    wide = src.bounds.right - src.bounds.left
    tall = src.bounds.top - src.bounds.bottom

    def round_ish(value):
        return value > 0 and abs(value - round(value, 3)) < 1e-9

    as_is = round_ish(wide / src.width) and round_ish(tall / src.height)
    fixed_x, fixed_y = wide / (src.width - 1), tall / (src.height - 1)
    if as_is or not (round_ish(fixed_x) and round_ish(fixed_y)):
        return None
    return (src.bounds.left - fixed_x / 2, src.bounds.bottom - fixed_y / 2,
            src.bounds.right + fixed_x / 2, src.bounds.top + fixed_y / 2)


def load_raster(path):
    """Every band of a GeoTIFF, masked, with its band labels and bounds.

    Masked rather than raw because the holes are NaN, and NaN propagates
    silently through arithmetic: a NaN forecast would give a NaN z, a NaN
    probability, and every comparison against it would read False rather
    than unknown. A masked array keeps those pixels out of the maths and
    out of the picture.

    A grid written half a cell off is corrected here rather than left to
    be noticed, since the numbers in the file are right and only its
    placement is wrong. It says so when it does, because a file quietly
    behaving differently from what is on disk is worth knowing about.
    """
    with rasterio.open(path) as src:
        data = src.read(masked=True)
        labels = [d if d else f"b{i}" for i, d in
                  enumerate(src.descriptions, start=1)]
        bounds = tuple(src.bounds)
        fixed = centre_registered(src)

    if fixed:
        cell = (fixed[2] - fixed[0]) / data.shape[2]
        print(f"{Path(path).name}: grid was written half a cell off, so "
              f"its cells read\n  as "
              f"{(bounds[2] - bounds[0]) / data.shape[2]:.4f} degrees "
              f"rather than {cell:g}. Corrected on read; the file on "
              f"disk\n  is untouched and the pixel values are unchanged.")
        bounds = fixed

    return data, labels, bounds


def to_grid(array, src_bounds, dst_bounds, dst_shape):
    """Put a coarse array onto a finer grid, nearest neighbour.

    The forecast is a 1 degree grid and rainfall is 0.05 degrees. There
    are two ways to put them together, and they do not give the same
    answer. Averaging rainfall down to 1 degree first smooths away most
    of the variation the drought threshold exists to capture, and leaves
    a country with only a few dozen cells in it. Carrying the forecast up
    instead keeps the threshold at the resolution it was measured at, and
    is what the Code Editor dashboard does, since Earth Engine resamples
    the coarse layer on demand rather than the fine one.

    Nearest neighbour rather than interpolation on purpose: the forecast
    really is constant across each of its cells, and smoothing it here
    would invent detail the model does not have.
    """
    left, bottom, right, top = src_bounds
    rows, cols = array.shape
    dleft, dbottom, dright, dtop = dst_bounds
    drows, dcols = dst_shape

    # Centres of the destination cells, in degrees.
    xs = dleft + (np.arange(dcols) + 0.5) * (dright - dleft) / dcols
    ys = dtop - (np.arange(drows) + 0.5) * (dtop - dbottom) / drows

    ci = np.clip(((xs - left) / (right - left) * cols).astype(int),
                 0, cols - 1)
    ri = np.clip(((top - ys) / (top - bottom) * rows).astype(int),
                 0, rows - 1)
    return array[np.ix_(ri, ci)]


def trigger_probability(reference, mu, var, percentile):
    """Chance of landing below the drought threshold, per pixel.

    reference is (years, rows, cols) and is whatever the threshold is taken
    from: observed rainfall where we have it, the hindcast otherwise. See
    calibration_check for why that choice matters.

    mu and var are (rows, cols).
    """
    # np.percentile has no masked version, so fill with NaN and use the
    # nan-aware one. A pixel masked in every year stays NaN and is masked
    # out again below. Those all-NaN columns are the ocean and the area
    # outside the model domain, so the warning about them is expected and
    # silenced rather than left to look like a fault.
    # "lower" rather than numpy's default, to match Earth Engine. The two
    # disagree by one year and it shows: with 43 years on record, the
    # default interpolates between the 9th and 10th driest, so 9 of 43
    # years sit below the line and the climatological probability comes
    # out at 0.209. Earth Engine returns the 9th driest itself, so 8 of
    # 43 sit below it and the answer is 0.186. One bucket apart on the
    # histogram, and it drags the conditional probability with it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        threshold = np.nanpercentile(reference.filled(np.nan), percentile,
                                     axis=0, method="lower")

    sd = np.sqrt(var)
    z = (threshold - mu) / sd
    prob = norm.cdf(z)

    # Anywhere the inputs were missing, the answer is missing too. Saying
    # "no danger" where there is no data would be worse than saying nothing.
    missing = np.isnan(threshold) | mu.mask | var.mask
    return (np.ma.array(threshold, mask=missing),
            np.ma.array(prob, mask=missing))


def calibration_check(hindcast, threshold, var, percentile):
    """Would a forecast carrying no information return the base rate?

    This is the one check worth running on this map, and it is cheap. Put
    the forecast exactly at the climatological mean, so it says nothing at
    all, and ask for the drought probability. A well-posed setup gives back
    the base rate: a 20th-percentile threshold should produce 0.20.

    It only does so when the spread used in the z-score matches the spread
    of the distribution the threshold was taken from. PyCPT's prediction
    error variance measures the model against OBSERVED rainfall, so the
    threshold has to come from observed rainfall too.

    A threshold taken from the hindcast fails this. The hindcast holds the
    model's own retrospective predictions, and predictions regress toward
    the mean, so their year-to-year spread is narrower than reality's. A
    percentile of that narrow distribution is not extreme enough, and every
    probability on the map comes out too high.

    Returns the no-signal probability. Compare it against the base rate.
    """
    clim = hindcast.mean(axis=0)
    return norm.cdf((threshold - clim) / np.sqrt(var))


def undefined_threshold(threshold, min_mm):
    """Pixels where a drought threshold cannot mean anything.

    Where a place gets almost no rain in the season, a percentile of its
    record sits at or near zero. Nothing can fall below zero, so the
    probability comes out as exactly 0, and the map shows the driest cells
    in the country as carrying no drought risk. The honest answer there is
    no answer.

    The test is the threshold itself rather than the average, because a
    pixel can average a respectable amount and still be zero in most
    years: a few wet seasons carry the mean while the 20th percentile sits
    on the floor. That is precisely the case this catches.

    Matters most somewhere largely desert, which is to say Yemen.
    """
    return np.ma.filled(np.ma.masked_invalid(threshold), 0.0) < min_mm


def climatological_probability(observed, threshold):
    """The base rate: how often history finished below the threshold.

    No model anywhere in this one. Count the years, divide. When the
    threshold is the Nth percentile of the same record, this comes back as
    N/100 almost everywhere by construction, which is exactly why it makes
    a good reference line on the chart: it is what you would say knowing
    nothing about this particular year.
    """
    below = (observed < threshold).sum(axis=0)
    return np.ma.array(below / observed.count(axis=0),
                       mask=np.ma.getmaskarray(observed).all(axis=0))


def analog_years(to_date, current_total, window_mm=5.0):
    """Past years that had about this much rain by this point.

    The dashboard's way of conditioning on the season so far. Rather than
    projecting each past year's remaining rain onto this one pixel by
    pixel, it picks out the past seasons that look like this one at the
    same point, within a few millimetres, and treats them as the set of
    ways this season could still go.

    The comparison is on the country average rather than per pixel, which
    the dashboard notes is for speed. It means every pixel is conditioned
    on the same set of years.

    Returns the matching years, and the value each was matched on.
    """
    return {year: value for year, value in to_date.items()
            if value is not None and abs(value - current_total) <= window_mm}


def comparison_values(curves, index):
    """Each past year's running total at the point this season reached.

    Reduces a set of curves to one number per year, which is all the
    matching above needs. Regional data arrives in this shape already,
    so both paths meet here.
    """
    out = {}
    for year, curve in curves.items():
        if curve:
            out[year] = curve[index] if index < len(curve) else curve[-1]
    return out


def conditional_probability(observed, threshold, rows):
    """How often the analog years finished below the threshold.

    No forecast in this one. Of the past seasons that had about this much
    rain by this point, count the share that ended up in drought. Early
    in a season almost every year qualifies and the answer sits near the
    base rate. Late in a season only a few do, and the answer is sharp.
    """
    subset = observed[rows]
    below = (subset < threshold).sum(axis=0)
    return np.ma.array(below / subset.count(axis=0),
                       mask=np.ma.getmaskarray(subset).all(axis=0))


def percent_departure(hindcast, mu):
    """Forecast against the hindcast average, as a percentage.

    Context for the probability map: it shows whether the season is forecast
    wet or dry, in the same language the monitoring slides already use.
    """
    mean = hindcast.mean(axis=0)
    return (mu - mean) / mean * 100.0


def upsample(array, factor):
    """A finer version of a coarse grid, edges included.

    Interpolating the values alone still leaves a staircase coastline,
    because the mask that says where data stops is itself only as fine as
    the model grid. So the mask is resampled too and re-cut at the halfway
    point, which gives a smooth outline.

    Missing cells are first filled from their nearest neighbour. Without
    that the interpolation would reach into the holes and drag their
    emptiness several cells inland, eating the coast.
    """
    # Nearest-neighbour fill, via the index of the closest valid cell.
    indices = ndimage.distance_transform_edt(
        np.ma.getmaskarray(array), return_distances=False,
        return_indices=True)
    filled = np.ma.getdata(array)[tuple(indices)]

    fine = ndimage.zoom(filled, factor, order=3)
    fine_mask = ndimage.zoom(
        np.ma.getmaskarray(array).astype(float), factor, order=1) > 0.5
    return np.ma.array(fine, mask=fine_mask)


def resolve_raster(path):
    """The file named, or its twin under the other TIFF spelling.

    .tif and .tiff are the same format, and which one you get depends on
    whoever wrote the file. A config naming one while the disk holds the
    other used to skip the forecast without erroring, so the deck came
    out missing its forecast slides with nothing to say why.
    """
    if path.exists():
        return path
    twin = path.with_suffix(".tiff" if path.suffix == ".tif" else ".tif")
    return twin if twin.exists() else path


def resolve_units(stored, wanted):
    """Turn what someone typed into admin unit ids.

    Names or ids both, because nobody should have to paste a 23-digit
    number to get a chart. Raises with the list when nothing matches,
    since a silent miss here means a deck quietly missing a region.
    """
    by_name = {str(v.get("name", "")).casefold(): key
               for key, v in stored.items()}
    found = []
    for ask in wanted:
        uid = ask if ask in stored else by_name.get(str(ask).casefold())
        if uid is None:
            known = "\n  ".join(sorted(v.get("name", k)
                                       for k, v in stored.items()))
            raise SystemExit(
                f"No admin unit matches '{ask}'.\n\nThe ones there are:"
                f"\n  {known}\n\n"
                f"build_deck.py --list-regions prints these with their ids."
            )
        found.append(uid)
    return found


def outline_rings(geojson, ids):
    """Every boundary ring of the named units, as lon/lat arrays.

    A unit is one Polygon or several, and each of those is an outer ring
    plus any holes. All of them are drawn: a hole is a border too, and
    Socotra being a separate polygon from the mainland is the normal
    case rather than an awkward one.
    """
    wanted, rings = set(ids), []
    for feature in (geojson or {}).get("features", []):
        if (feature.get("properties") or {}).get("id") not in wanted:
            continue
        geometry = feature.get("geometry") or {}
        polygons = ([geometry.get("coordinates")]
                    if geometry.get("type") == "Polygon"
                    else geometry.get("coordinates") or [])
        for polygon in polygons:
            for ring in polygon or []:
                if len(ring) > 2:
                    rings.append(np.asarray(ring, dtype=float))
    return rings


def ring_area_and_centre(ring):
    """Signed-area maths for one closed ring: its size, and its centroid.

    The centroid rather than the middle of the bounding box, because a
    crescent-shaped governorate has a bounding box whose middle can sit
    outside it altogether.
    """
    x, y = ring[:, 0], ring[:, 1]
    cross = x[:-1] * y[1:] - x[1:] * y[:-1]
    area = cross.sum() / 2.0
    if abs(area) < 1e-12:
        return 0.0, (float(x.mean()), float(y.mean()))
    cx = ((x[:-1] + x[1:]) * cross).sum() / (6.0 * area)
    cy = ((y[:-1] + y[1:]) * cross).sum() / (6.0 * area)
    return abs(area), (float(cx), float(cy))


def outline_labels(geojson, ids, names):
    """Where to write each selected unit's name on the map.

    One label per unit, placed at the centre of its largest piece, so a
    governorate with offshore islands is named once on the mainland
    rather than once per island. "Governorate" is dropped from the text:
    on a map of governorates it says nothing, and the names are long
    enough to collide without it.
    """
    wanted, labels = set(ids), []
    for feature in (geojson or {}).get("features", []):
        uid = (feature.get("properties") or {}).get("id")
        if uid not in wanted:
            continue
        geometry = feature.get("geometry") or {}
        polygons = ([geometry.get("coordinates")]
                    if geometry.get("type") == "Polygon"
                    else geometry.get("coordinates") or [])
        best = None
        for polygon in polygons:
            if not polygon or len(polygon[0]) < 4:
                continue
            area, centre = ring_area_and_centre(
                np.asarray(polygon[0], dtype=float))
            if best is None or area > best[0]:
                best = (area, centre)
        if best is None:
            continue
        text = str(names.get(uid, uid)).replace(" Governorate", "").strip()
        labels.append((best[1][0], best[1][1], text))
    return labels


def render(array, path, palette, vmin, vmax, smooth=True, bounds=None,
           outlines=None, labels=None):
    """Save a bare map image, the same shape of thing Earth Engine returns.

    No title and no colour bar: build_deck.py draws those, so that the
    forecast panels are composed exactly like every other panel.

    The PyCPT grid is coarse, 0.25 degrees, so drawn one square per cell it
    comes out visibly blocky next to the CHIRPS maps on the other slides,
    which are five times finer. Interpolating smooths that out and matches
    how the same data looks in the Code Editor, which resamples on display.

    Set smooth=False to see the model's actual resolution. The smoothed
    version is easier to read but flatters the data, so it is worth looking
    at the honest one at least once.
    """
    cmap = LinearSegmentedColormap.from_list(
        "forecast", ["#" + c for c in palette])
    cmap.set_bad(alpha=0.0)          # missing stays transparent

    # Only worth doing on a coarse grid. On the rainfall grid the map is
    # already finer than the page, and blowing it up eightfold would cost
    # a lot of memory to change nothing anyone can see.
    if smooth and array.shape[1] < 120:
        array = upsample(array, UPSAMPLE)

    # Longest side eight inches, so the file is a sane size whatever the
    # grid is, and the aspect ratio is the map's own.
    rows, cols = array.shape
    longest = max(rows, cols)
    fig = plt.figure(figsize=(8 * cols / longest, 8 * rows / longest),
                     dpi=220)
    # With bounds the axes are in degrees, so the borders can be drawn
    # in the coordinates they come in. aspect="auto" keeps the image
    # filling the figure exactly as it did before, since the figure is
    # already sized to the grid.
    extent = None
    if bounds is not None:
        left, bottom, right, top = bounds
        extent = (left, right, bottom, top)

    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax, extent=extent,
              aspect="auto",
              interpolation="bilinear" if smooth else "nearest")

    for ring in outlines or []:
        ax.plot(ring[:, 0], ring[:, 1], color="#000000", linewidth=1.3,
                solid_joinstyle="round", solid_capstyle="round")
    # Black text with a white edge, so the name reads against both the
    # navy and the red end of the colour ramp. Only when the axes are in
    # degrees, since the positions are longitudes and latitudes.
    if extent is not None:
        for x, y, text in labels or []:
            ax.text(x, y, text, ha="center", va="center", fontsize=11,
                    fontweight="bold", color="#000000",
                    path_effects=[patheffects.withStroke(
                        linewidth=2.5, foreground="#ffffff")])
        ax.set_xlim(extent[0], extent[1])
        ax.set_ylim(extent[2], extent[3])

    ax.set_axis_off()
    fig.savefig(path, dpi=220, transparent=True)
    plt.close(fig)


def cell_area_km2(bounds, shape):
    """Roughly how many square kilometres one grid cell covers.

    Only used to put the histogram on the dashboard's axis, so the
    flat-earth approximation at the middle latitude is plenty.
    """
    left, bottom, right, top = bounds
    rows, cols = shape
    middle = math.radians((bottom + top) / 2)
    wide = (right - left) / cols * 111.32 * math.cos(middle)
    tall = (top - bottom) / rows * 110.57
    return wide * tall


def chart_pair(raw_dir, spec, reference, ref_years, threshold, mu, var,
               prob, percentile, grid_bounds, series, to_date, slug,
               season_start=None, season_length=None, label=None):
    """Both charts for one area, which may be a country or one unit of it.

    Everything that differs between areas arrives as an argument. The
    rasters come in already cut to the area, and series and to_date are
    that area's own rainfall, so nothing in here needs to know which it
    is drawing. That is what lets a governorate and the whole country be
    built by the same code and come out comparable.

    Returns the panels, and a short summary of the numbers behind them.
    """
    panels = []
    width = spec.get("chart_width", 4.30)
    height = spec.get("chart_height", 3.20)

    # ---- chart 1: how the season is tracking ----
    # The band is the spatial mean of each pixel's own interval rather
    # than an interval on the mean. That treats the model's errors as
    # moving together across the area, which for a seasonal signal is
    # nearer the truth than assuming they cancel, and errs wide.
    z = spec.get("bounds_z", 1.0)
    sd = np.sqrt(var)
    forecast_mm = float(mu.mean())
    threshold_mm = float(threshold.mean())

    name = f"forecast_season_to_date{slug}.png"
    season_to_date(
        raw_dir / name,
        days=[d for d, _ in series],
        observed_mm=[v for _, v in series],
        forecast_mm=forecast_mm,
        lower_mm=max(0.0, float((mu - z * sd).mean())),
        upper_mm=float((mu + z * sd).mean()),
        threshold_mm=threshold_mm,
        title="Forecast & Rainfall So Far This Year",
        width=width, height=height,
        season_start=season_start, season_length=season_length,
    )
    panels.append({"key": "season_to_date", "image": name, "composed": True})

    # ---- chart 2: the three probabilities side by side ----
    clim = climatological_probability(reference, threshold)
    distributions = [("Clim. prob", clim.compressed(), CLIM)]

    has_conditional, matched = False, []
    window = spec.get("analog_window_mm", 5.0)
    so_far = series[-1][1] if series else None
    if so_far is not None and to_date:
        matches = analog_years(to_date, so_far, window)
        rows = [i for i, year in enumerate(ref_years)
                if str(year).isdigit() and int(year) in matches]
        if rows:
            cond = conditional_probability(reference, threshold, rows)
            distributions.append(("Condtl. prob", cond.compressed(), CONDTL))
            has_conditional, matched = True, sorted(matches)

    distributions.append(("Fcast prob", prob.compressed(), FCAST_PROB))

    # Counted as 1 km pixels, which is what the dashboard's histogram
    # does, so the two can be read against each other.
    weight = 1.0
    if spec.get("histogram_as_km2", True):
        weight = cell_area_km2(grid_bounds, threshold.shape)

    name = f"forecast_probabilities{slug}.png"
    probability_distributions(raw_dir / name, distributions,
                              title="Trigger Probabilities",
                              width=width, height=height, weight=weight)
    panels.append({"key": "probabilities", "image": name,
                   "composed": True, "has_conditional": has_conditional})

    summary = {
        "label": label,
        "threshold_mm": round(threshold_mm, 1),
        "forecast_mm": round(forecast_mm, 1),
        "observed_mm": round(so_far, 1) if so_far is not None else None,
        "cells": int(threshold.count()),
        "clim_probability": round(float(clim.mean()), 3),
        "fcast_probability": round(float(prob.mean()), 3),
        "analog_years": matched,
        "panels": panels,
        "has_conditional": has_conditional,
    }
    if has_conditional:
        summary["conditional_probability"] = round(float(cond.mean()), 3)
    return panels, summary


def report_area(summary, percentile):
    """One block of numbers per area, so a run can be read at a glance."""
    print(f"\n{summary['label'] or 'Whole region'}  "
          f"({summary['cells']:,} cells)")
    line = (f"  threshold {summary['threshold_mm']:.1f} mm, "
            f"forecast {summary['forecast_mm']:.1f} mm")
    if summary["observed_mm"] is not None:
        line += f", fallen so far {summary['observed_mm']:.1f} mm"
    print(line)
    print(f"  climatological {summary['clim_probability']:.3f} "
          f"(should be about {baseline(percentile):.2f}), "
          f"forecast {summary['fcast_probability']:.3f}")
    if summary["has_conditional"]:
        print(f"  conditional {summary['conditional_probability']:.3f}, "
              f"from {len(summary['analog_years'])} analog years")
    elif summary["observed_mm"] is not None:
        print(f"  no analog years within the window, so no conditional "
              f"probability")


def load_observed(base_dir, config):
    """observed.json, or nothing if observed.py has not been run."""
    path = base_dir / config["output"]["dir"] / "observed.json"
    if not path.exists():
        print("\nNo observed.json, so no charts this run. "
              "Run observed.py to pull CHIRPS.")
        return None
    return json.loads(path.read_text())


def build_charts(obs, spec, raw_dir, reference, ref_years, threshold, mu,
                 var, prob, percentile, grid_bounds):
    """The whole-country pair."""
    series = obs.get("current_cumulative") or []
    # Keyed by year. Anything else means observed.json was written by a
    # version that mixed up its variables, so say which file to rebuild
    # rather than failing on an int() a long way from the cause.
    raw_history = obs.get("historical_curves") or {}
    bad = [k for k in raw_history if not str(k).isdigit()]
    if bad:
        raise SystemExit(
            f"observed.json has historical_curves keyed by "
            f"'{bad[0]}' rather than by year.\nRe-run observed.py to "
            f"write it again."
        )
    history = {int(y): c for y, c in raw_history.items()}
    if not series and not history:
        return [], None

    # The dashboard compares this season's running total against each
    # past year's total one pentad earlier, because it works the cut off
    # date out in days and then filters on a half open range that drops
    # the pentad landing on it. Matched on purpose so the two agree. Set
    # analog_offset_pentads to 0 to compare like with like.
    offset = spec.get("analog_offset_pentads", 1)
    index = max(0, len(series) - 1 - offset)
    to_date = comparison_values(history, index)

    panels, summary = chart_pair(
        raw_dir, spec, reference, ref_years, threshold, mu, var, prob,
        percentile, grid_bounds, series, to_date, slug="",
        season_start=obs.get("season_start"),
        season_length=obs.get("season_length"),
        label=obs.get("region"))
    report_area(summary, percentile)
    return panels, summary


def build_regional_charts(base_dir, obs, spec, raw_dir, reference,
                          ref_years, threshold, mu, var, prob, percentile,
                          grid_bounds, wanted):
    """One pair of charts per requested admin unit.

    The rasters are the national ones with a second mask laid over them,
    so a governorate's threshold and probabilities are computed from the
    same numbers as the country's, just counted over fewer pixels. The
    rainfall curves come from observed.json, which holds one per unit.
    """
    stored = obs.get("regions") or {}
    masks_path = base_dir / spec.get("dir", "forecasts") / "region_masks.tif"
    if not stored or not masks_path.exists():
        raise SystemExit(
            "No per-region rainfall on disk, so these cannot be drawn.\n"
            "Set regional_data: true in config.yaml and run observed.py "
            "again."
        )

    masks, mask_ids, mask_bounds = load_raster(masks_path)
    if masks.shape[1:] != threshold.shape:
        raise SystemExit(
            f"{masks_path.name} is on a different grid from the rainfall. "
            f"Re-run observed.py."
        )

    by_id = {str(name): i for i, name in enumerate(mask_ids)}

    groups = []
    for uid in resolve_units(stored, wanted):
        if uid not in by_id:
            raise SystemExit(
                f"{stored[uid].get('name', uid)} is in observed.json but "
                f"not in {masks_path.name}.\nThey are written by the same "
                f"run, so re-run observed.py to put them back in step."
            )
        outside = np.ma.getmaskarray(masks)[by_id[uid]]
        if (~outside).sum() == 0:
            print(f"\n{stored[uid].get('name', uid)}: no cells on this "
                  f"grid, skipped. The unit is smaller than one cell.")
            continue

        def cut(array):
            if array.ndim == 3:
                return np.ma.array(
                    np.ma.getdata(array),
                    mask=np.ma.getmaskarray(array) | outside[None, :, :])
            return np.ma.array(np.ma.getdata(array),
                               mask=np.ma.getmaskarray(array) | outside)

        unit = stored[uid]
        to_date = {int(y): v
                   for y, v in (unit.get("to_date_by_year") or {}).items()
                   if v is not None}
        panels, summary = chart_pair(
            raw_dir, spec, cut(reference), ref_years, cut(threshold),
            cut(mu), cut(var), cut(prob), percentile, grid_bounds,
            unit.get("current_cumulative") or [], to_date,
            slug=f"_{uid}",
            season_start=obs.get("season_start"),
            season_length=obs.get("season_length"),
            label=unit.get("name", uid))
        summary["id"] = uid
        report_area(summary, percentile)
        groups.append(summary)

    return groups


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn a PyCPT forecast into drought maps and charts.")
    parser.add_argument(
        "--regions", nargs="+", metavar="ID", default=[],
        help="admin units to chart, by name or by id. Given, these "
             "replace the national charts rather than being added to "
             "them. build_deck.py --list-regions prints the options.")
    args = parser.parse_args(argv)
    regions_wanted = args.regions

    base_dir = Path(__file__).parent
    with open(base_dir / "config.yaml") as f:
        config = yaml.safe_load(f)

    spec = config.get("forecasts") or {}
    needed = ("forecast_mu", "forecast_var")
    if not all(spec.get(k) for k in needed):
        print("No forecast files configured, skipping the forecast slide.")
        print(f"  (set {', '.join(needed)} under 'forecasts' in config.yaml)")
        return

    out_dir = base_dir / config["output"]["dir"]
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "forecast.json"

    paths = {k: resolve_raster(base_dir / spec[k]) for k in needed}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        print("Forecast files are configured but not on disk, skipping:")
        for p in missing:
            print(f"  {p}")
        # Clear any previous run's output. Leaving it would let the deck
        # quietly rebuild with the last forecast that worked, which looks
        # exactly like a successful run and is the worst way to be wrong.
        if stale.exists():
            stale.unlink()
            print("\n  Removed the previous forecast.json, so the deck does "
                  "not silently\n  reuse an older forecast. The forecast "
                  "slides will be absent until\n  these files are in place.")
        return

    mu_all, mu_labels, mu_bounds = load_raster(paths["forecast_mu"])
    var_all, _, var_bounds = load_raster(paths["forecast_var"])
    mu, var = mu_all[0], var_all[0]
    grid_bounds = mu_bounds

    # Unit corrections, applied before anything else uses these.
    #
    # The Code Editor forecast script divides the mean by 3 and the
    # variance by 9, which is one rescaling applied consistently: scale a
    # quantity by 1/3 and its variance goes by 1/9. Whether either is
    # needed depends on how the NetCDF was written, so neither is assumed.
    # The calibration check below is how you tell: it reports what a
    # forecast carrying no information returns, and that should be the
    # base rate.
    mean_scale = spec.get("mean_scale", 1.0)
    variance_scale = spec.get("variance_scale", 1.0)
    if mean_scale != 1.0 or variance_scale != 1.0:
        mu = mu * mean_scale
        var = var * variance_scale
        print(f"Scaled the forecast mean by {mean_scale:g} and the "
              f"variance by {variance_scale:g}")

    print(f"Forecast:  {mu_labels[0]}")
    print(f"Extent:    {tuple(round(v, 2) for v in mu_bounds)}")

    if mu_bounds != var_bounds:
        raise SystemExit(
            "The forecast mean and variance cover different areas, so they "
            "cannot be combined. Reproject them onto one grid first."
        )

    # A forecast carrying nothing will sail through every later step and
    # produce a blank map that looks like a successful run, so check it
    # holds real numbers before going on.
    #
    # The failure this catches is a conversion from the PyCPT NetCDF that
    # wrote an integer raster. Rainfall is a float, and NaN cast to an
    # unsigned integer becomes that type's maximum, so every cell ends up
    # holding the same enormous value. Nothing is marked nodata, because
    # from the file's point of view every cell has a number in it.
    for label, array, path in (("mean", mu, paths["forecast_mu"]),
                               ("variance", var, paths["forecast_var"])):
        trouble = None
        if array.count() == 0:
            trouble = "every cell is nodata"
        elif array.min() == array.max():
            trouble = (f"every cell holds the same value, "
                       f"{float(array.min()):.0f}")
        if trouble:
            raise SystemExit(
                f"The forecast {label} carries no usable data: {trouble}.\n"
                f"  {path}\n\n"
                f"This normally means the conversion from the PyCPT NetCDF\n"
                f"failed. Check the file is a float type rather than an\n"
                f"integer, and that it has a CRS. Converting the .nc again\n"
                f"with the PyCPT-to-GeoTIFF script should fix it."
            )

    # Everything from here runs on the analysis grid, which is rainfall
    # resolution rather than the forecast's. observed.py writes the mask
    # and the rainfall on that grid, so the mask defines it, and the
    # forecast is carried up onto it. See to_grid for why that way round.
    #
    # The mask also clips to the country: PyCPT output is a rectangle
    # that spills well past it, and for Yemen that rectangle holds Saudi
    # Arabia, Oman, the Red Sea and part of the Horn of Africa.
    # Set once the forecast has been carried onto a finer grid than its
    # own, which changes what the calibration check below can tell you.
    mixed_grid = False
    coarse_deg = fine_deg = None

    mask_path = spec.get("region_mask")
    if mask_path and (base_dir / mask_path).exists():
        mask_raster, _, grid_bounds = load_raster(base_dir / mask_path)
        outside = np.ma.getmaskarray(mask_raster)[0]

        if mask_raster.shape[1:] != mu.shape:
            mixed_grid = True
            mu = to_grid(mu, mu_bounds, grid_bounds, outside.shape)
            var = to_grid(var, var_bounds, grid_bounds, outside.shape)
            fine_deg = (grid_bounds[2] - grid_bounds[0]) / outside.shape[1]
            coarse_deg = (mu_bounds[2] - mu_bounds[0]) / mu_all.shape[2]
            print(f"Forecast carried from {coarse_deg:g} degree cells onto "
                  f"the {fine_deg:g} degree rainfall grid")

        kept = int((~outside).sum())
        print(f"Clipped to {kept} of {outside.size} cells inside the region "
              f"({kept / outside.size * 100:.0f}% of the rectangle)")
        mu = np.ma.array(np.ma.getdata(mu),
                         mask=np.ma.getmaskarray(mu) | outside)
        var = np.ma.array(np.ma.getdata(var),
                          mask=np.ma.getmaskarray(var) | outside)
    else:
        outside = None
        print("No region mask, so the maps cover the whole forecast "
              "rectangle,\n  which extends past the country, and the "
              "forecast stays on its own\n  coarse grid. Run observed.py "
              "to produce one.")

    # The hindcast is optional now that the threshold comes from observed
    # rainfall. It is only still used as the baseline for the departure
    # map, and observed rainfall does that job better anyway: departure
    # from what actually falls means more than departure from what the
    # model used to predict.
    hindcast, years = None, None
    if spec.get("hindcast_mu") and (base_dir / spec["hindcast_mu"]).exists():
        hindcast, years, hc_bounds = load_raster(base_dir / spec["hindcast_mu"])
        if hindcast.shape[1:] != mu.shape:
            hindcast = np.ma.stack(
                [to_grid(band, hc_bounds, grid_bounds, mu.shape)
                 for band in hindcast])
        print(f"Hindcast:  {hindcast.shape[0]} years, "
              f"{years[0]} to {years[-1]}")

    percentile = spec.get("threshold_percentile", 20)

    # Where the drought threshold comes from. Observed rainfall is correct;
    # the hindcast is a stand-in that biases the result high, and the
    # calibration check below measures by how much.
    observed_path = spec.get("observed_rainfall")
    if observed_path and (base_dir / observed_path).exists():
        reference, ref_years, ref_bounds = load_raster(base_dir / observed_path)
        if reference.shape[1:] != mu.shape:
            raise SystemExit(
                f"{observed_path} is on a different grid from the region "
                f"mask.\n  rainfall  {reference.shape[1:]} over {ref_bounds}"
                f"\n  mask      {mu.shape} over {grid_bounds}\n"
                f"Both are written by observed.py, so re-run it to put "
                f"them back in step."
            )
        if outside is not None:
            reference = np.ma.array(
                np.ma.getdata(reference),
                mask=np.ma.getmaskarray(reference) | outside[None, :, :])
        source = f"observed rainfall, {ref_years[0]}-{ref_years[-1]}"
    elif hindcast is not None:
        reference, ref_years = hindcast, years
        source = f"the hindcast, {years[0]}-{years[-1]}"
    else:
        raise SystemExit(
            "No rainfall record to take the drought threshold from.\n"
            "Run observed.py to pull CHIRPS, or point forecasts."
            "hindcast_mu at a hindcast file."
        )

    threshold, prob = trigger_probability(reference, mu, var, percentile)

    # Somewhere that gets no rain cannot have a rainfall deficit, so those
    # pixels are left blank rather than reported as carrying no risk.
    min_mm = spec.get("min_threshold_mm") or 0
    undefined = undefined_threshold(threshold, min_mm)
    dropped = int((undefined & ~np.ma.getmaskarray(threshold)).sum())
    if dropped:
        print(f"  {dropped} pixels have a threshold under {min_mm} mm and "
              f"are left blank:\n  nothing can fall below a threshold of "
              f"almost zero, so the probability there is not low, it is "
              f"undefined.")

    def blank_out(array):
        return np.ma.array(np.ma.getdata(array),
                           mask=np.ma.getmaskarray(array) | undefined)

    threshold, prob = blank_out(threshold), blank_out(prob)
    reference = np.ma.array(
        np.ma.getdata(reference),
        mask=np.ma.getmaskarray(reference) | undefined[None, :, :])

    # Keep both maps covering the same ground.
    # Departure from observed normal where we have it, which is the
    # more meaningful baseline, falling back to the hindcast mean.
    baseline_source = reference if hindcast is None else hindcast
    departure = blank_out(percent_departure(baseline_source, mu))

    print(f"\nThreshold source:     {source}")
    print(f"Threshold ({percentile}th percentile): "
          f"{threshold.mean():.1f} mm on average")
    print(f"Forecast mean:        {mu.mean():.1f} mm")
    print(f"Drought probability:  {prob.mean():.3f} on average "
          f"(baseline {baseline(percentile):.2f})")
    share = (prob > baseline(percentile)).sum() / prob.count() * 100
    print(f"  {share:.0f}% of pixels sit above the baseline")

    # The check that decides whether this map can be read as a probability
    # at all. See calibration_check for what it means.
    null_prob = calibration_check(reference, threshold, var, percentile)
    null_mean = float(np.ma.array(null_prob, mask=mu.mask).mean())
    print(f"\nCalibration: a forecast carrying no information returns "
          f"{null_mean:.3f}")
    print(f"  it should return the base rate, {baseline(percentile):.2f}")

    drift = null_mean - baseline(percentile)
    calibrated = abs(drift) <= 0.05

    # The check only means what it says when the forecast and the
    # rainfall are on one grid. They are not here, on purpose: the
    # forecast's error variance describes how wrong a 1 degree average
    # is, while each 0.05 degree pixel's own year-to-year spread is far
    # wider than any 1 degree average's. Dividing one by the other is
    # not a like for like comparison, and the number will look off
    # however well set up the forecast is. So it is reported and not
    # acted on, rather than marking the slide over it.
    flag_on_slide = not calibrated and not mixed_grid
    if mixed_grid:
        print(f"  not comparable on this run: the forecast is a "
              f"{coarse_deg:g} degree quantity and\n  the rainfall is "
              f"{fine_deg:g} degree, so the two spreads are not the same "
              f"kind of thing.\n  Reported for the record, not read as a "
              f"fault.")
    elif not calibrated:
        direction = "high" if drift > 0 else "low"
        print(f"\n  WARNING: off by {drift:+.3f}, so every probability on "
              f"this map reads {direction}.")
        if not observed_path:
            print("  The threshold is coming from the hindcast, which holds "
                  "the model's own")
            print("  predictions. Those regress toward the mean, so their "
                  "spread is narrower")
            print("  than real rainfall and the percentile is not extreme "
                  "enough. PyCPT's")
            print("  variance is measured against observations, so the "
                  "threshold needs to be")
            print("  too. Set 'observed_rainfall' in config.yaml to a "
                  "GeoTIFF of observed")
            print("  seasonal totals on this same grid, one band per year.")
        print("  Treat the map as indicative until that is fixed.")

    prob_palette = spec.get("probability_palette",
                            ["001137", "0aab1e", "e7eb05", "ff4a2d", "e90000"])
    dep_palette = spec.get("departure_palette",
                           ["B78200", "FFFFFF", "0027FF"])

    season = spec.get("season", "")
    panels = [
        {
            "key": "trigger_probability",
            "raw_image": "forecast_trigger_probability.png",
            "array": prob,
            "palette": prob_palette,
            "min": 0.0,
            "max": 0.5,
            "title": f"Probability of drought danger conditions, {season}",
            "units": (f"probability | {baseline(percentile):.2f} is the "
                      f"long-run base rate"),
        },
        {
            "key": "percent_departure",
            "raw_image": "forecast_percent_departure.png",
            "array": departure,
            "palette": dep_palette,
            "min": -100,
            "max": 100,
            "title": f"Forecast rainfall, % departure from normal, {season}",
            "units": ("percent departure from the hindcast average"
                      if hindcast is not None else
                      "percent departure from the observed average"),
        },
    ]

    # Outline whichever regions were asked for, and only those. A map
    # with all 22 governorates drawn on it is a different kind of map
    # and nobody asked for that one.
    obs = load_observed(base_dir, config)
    outlines, outlined, labels = [], [], []
    if regions_wanted:
        stored = (obs or {}).get("regions") or {}
        shapes = base_dir / spec.get("dir", "forecasts") / "regions.geojson"
        if stored and shapes.exists():
            outlined = resolve_units(stored, regions_wanted)
            borders = json.loads(shapes.read_text())
            outlines = outline_rings(borders, outlined)
            labels = outline_labels(
                borders, outlined,
                {u: stored[u].get("name", u) for u in outlined})
            names = ", ".join(stored[u].get("name", u) for u in outlined)
            print(f"\nOutlining on the maps: {names}")
        else:
            print(f"\nNo region borders on disk, so the maps are drawn "
                  f"without them.\n  Re-run observed.py, which writes "
                  f"{shapes.name}.")

    smooth = spec.get("smooth_maps", True)
    for panel in panels:
        render(panel.pop("array"), raw_dir / panel["raw_image"],
               panel["palette"], panel["min"], panel["max"], smooth=smooth,
               bounds=grid_bounds, outlines=outlines, labels=labels)

    # The charts. Asked for admin units, those replace the national pair
    # rather than joining it: a deck about three governorates should not
    # open with a country-wide chart nobody asked for. The maps above
    # stay national either way, since a single governorate drawn on its
    # own is too small to read.
    # Clear out the last run's charts first. Without this, output/raw
    # keeps the charts for whichever governorates were asked for last
    # time, none of which are in this deck, and nobody looking in that
    # folder afterwards can tell which belong to the current bulletin.
    gone = 0
    for pattern in ("forecast_season_to_date*.png",
                    "forecast_probabilities*.png"):
        for old in raw_dir.glob(pattern):
            old.unlink()
            gone += 1
    if gone:
        print(f"\nCleared {gone} chart{'' if gone == 1 else 's'} from the "
              f"previous run")

    chart_panels, regional = [], []
    if obs is not None:
        if regions_wanted:
            regional = build_regional_charts(
                base_dir, obs, spec, raw_dir, reference, ref_years,
                threshold, mu, var, prob, percentile, grid_bounds,
                regions_wanted)
        else:
            chart_panels, _ = build_charts(
                obs, spec, raw_dir, reference, ref_years, threshold, mu,
                var, prob, percentile, grid_bounds)

    note = spec.get("note", "")
    if flag_on_slide:
        # Say so on the slide itself, not only in the terminal. A map
        # that reads wrong should say so where it will be read. The
        # wording has to match what is actually the matter, since this
        # ends up in front of people who will not see the terminal.
        direction = "upward" if drift > 0 else "downward"
        if not observed_path:
            why = ("the drought threshold is taken from the hindcast "
                   "rather than observed rainfall")
        else:
            why = ("the forecast's spread does not match the spread of "
                   "the rainfall record the threshold came from")
        note = (f"{note}  Provisional: {why}, which biases the "
                f"probabilities {direction}. Read as indicative.").strip()

    record = {
        "heading": spec.get("heading", "Seasonal Forecast"),
        "season": season,
        "note": note,
        "source_detail": spec.get("source_detail", ""),
        "threshold_source": source,
        "calibrated": calibrated,
        "calibration_comparable": not mixed_grid,
        "null_probability": round(null_mean, 3),
        "hindcast_start": years[0] if years else None,
        "hindcast_end": years[-1] if years else None,
        "forecast_year": mu_labels[0],
        "threshold_percentile": percentile,
        "extent": [round(v, 4) for v in grid_bounds],
        "panels": panels,
        "chart_panels": chart_panels,
        "has_conditional": any(p.get("has_conditional")
                               for p in chart_panels),
        # One entry per requested admin unit, each carrying its own pair
        # of charts and the numbers behind them. Empty on a normal run.
        "regional_charts": regional,
        # What was asked for, so build_deck.py can tell whether this
        # file still answers the question being put to it.
        "regions_requested": list(regions_wanted),
    }
    (out_dir / "forecast.json").write_text(json.dumps(record, indent=2))

    def plural(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")

    drawn = len(chart_panels) + sum(len(g["panels"]) for g in regional)
    print(f"\nWrote {plural(len(panels), 'map')} and "
          f"{plural(drawn, 'chart')} to {raw_dir}/, "
          f"and forecast.json to {out_dir}/")
    if regional:
        print(f"  {plural(len(regional), 'region')}: "
              + ", ".join(g["label"] for g in regional))
    print("Next: python build_deck.py"
          + (f" --regions {' '.join(regions_wanted)}" if regions_wanted
             else ""))


if __name__ == "__main__":
    main()