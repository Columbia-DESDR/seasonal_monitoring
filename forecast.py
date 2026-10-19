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

import json
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import yaml
from matplotlib.colors import LinearSegmentedColormap
from scipy import ndimage
from scipy.stats import norm


# How much finer to draw the map than the model grid. Presentation only:
# it changes nothing about the numbers, it just stops a 0.25-degree grid
# from reading as a mosaic next to the far finer CHIRPS maps.
UPSAMPLE = 8


# The baseline probability implied by the threshold. A 20th-percentile
# threshold means a year we know nothing about has a 20% chance of falling
# below it, so this is what the map is read against.
def baseline(percentile):
    return percentile / 100.0


def load_raster(path):
    """Every band of a GeoTIFF, masked, with its band labels and bounds.

    Masked rather than raw because the holes are NaN, and NaN propagates
    silently through arithmetic: a NaN forecast would give a NaN z, a NaN
    probability, and every comparison against it would read False rather
    than unknown. A masked array keeps those pixels out of the maths and
    out of the picture.
    """
    with rasterio.open(path) as src:
        data = src.read(masked=True)
        labels = [d if d else f"b{i}" for i, d in
                  enumerate(src.descriptions, start=1)]
        bounds = tuple(src.bounds)
    return data, labels, bounds


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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        threshold = np.nanpercentile(reference.filled(np.nan), percentile,
                                     axis=0)

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


def render(array, path, palette, vmin, vmax, smooth=True):
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

    if smooth:
        array = upsample(array, UPSAMPLE)

    rows, cols = array.shape
    fig = plt.figure(figsize=(cols / 60, rows / 60), dpi=220)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax,
              interpolation="bilinear" if smooth else "nearest")
    ax.set_axis_off()
    fig.savefig(path, dpi=220, transparent=True)
    plt.close(fig)


def main():
    base_dir = Path(__file__).parent
    with open(base_dir / "config.yaml") as f:
        config = yaml.safe_load(f)

    spec = config.get("forecasts") or {}
    needed = ("hindcast_mu", "forecast_mu", "forecast_var")
    if not all(spec.get(k) for k in needed):
        print("No forecast files configured, skipping the forecast slide.")
        print(f"  (set {', '.join(needed)} under 'forecasts' in config.yaml)")
        return

    paths = {k: base_dir / spec[k] for k in needed}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        print("Forecast files are configured but not on disk, skipping:")
        for p in missing:
            print(f"  {p}")
        return

    out_dir = base_dir / config["output"]["dir"]
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    hindcast, years, hc_bounds = load_raster(paths["hindcast_mu"])
    mu_all, mu_labels, mu_bounds = load_raster(paths["forecast_mu"])
    var_all, _, var_bounds = load_raster(paths["forecast_var"])
    mu, var = mu_all[0], var_all[0]

    print(f"Hindcast:  {hindcast.shape[0]} years, "
          f"{years[0]} to {years[-1]}")
    print(f"Forecast:  {mu_labels[0]}")
    print(f"Extent:    {tuple(round(v, 2) for v in hc_bounds)}")

    if hc_bounds != mu_bounds or hc_bounds != var_bounds:
        raise SystemExit(
            "The hindcast and forecast cover different areas, so they cannot "
            "be compared pixel by pixel. Reproject them onto one grid first."
        )

    percentile = spec.get("threshold_percentile", 20)

    # Where the drought threshold comes from. Observed rainfall is correct;
    # the hindcast is a stand-in that biases the result high, and the
    # calibration check below measures by how much.
    observed_path = spec.get("observed_rainfall")
    if observed_path and (base_dir / observed_path).exists():
        reference, ref_years, ref_bounds = load_raster(base_dir / observed_path)
        if ref_bounds != hc_bounds:
            raise SystemExit(
                f"{observed_path} is on a different grid from the forecast. "
                "Reproject it onto the forecast grid first."
            )
        source = f"observed rainfall, {ref_years[0]}-{ref_years[-1]}"
    else:
        reference = hindcast
        source = f"the hindcast, {years[0]}-{years[-1]}"

    threshold, prob = trigger_probability(reference, mu, var, percentile)
    departure = percent_departure(hindcast, mu)

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
    if not calibrated:
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
            "units": "percent departure from the hindcast average",
        },
    ]

    smooth = spec.get("smooth_maps", True)
    for panel in panels:
        render(panel.pop("array"), raw_dir / panel["raw_image"],
               panel["palette"], panel["min"], panel["max"], smooth=smooth)

    note = spec.get("note", "")
    if not calibrated:
        # Say so on the slide itself, not only in the terminal. A map that
        # reads high should say that where it will be read.
        note = (note + "  Provisional: the drought threshold is taken from "
                "the hindcast rather than observed rainfall, which biases "
                "the probabilities upward. Read as indicative.").strip()

    record = {
        "heading": spec.get("heading", "Seasonal Forecast"),
        "season": season,
        "note": note,
        "source_detail": spec.get("source_detail", ""),
        "threshold_source": source,
        "calibrated": calibrated,
        "null_probability": round(null_mean, 3),
        "hindcast_start": years[0],
        "hindcast_end": years[-1],
        "forecast_year": mu_labels[0],
        "threshold_percentile": percentile,
        "extent": [round(v, 4) for v in hc_bounds],
        "panels": panels,
    }
    (out_dir / "forecast.json").write_text(json.dumps(record, indent=2))
    print(f"\nWrote 2 maps to {raw_dir}/ and forecast.json to {out_dir}/")
    print("Next: python build_deck.py")


if __name__ == "__main__":
    main()
