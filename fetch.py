"""
Pull the bulletin maps out of Google Earth Engine.

This is the Python version of the yemen_*_monitoring scripts. One difference
worth knowing: those render maps and push them to Google Drive, where somebody
downloads them by hand. Here we ask Earth Engine for the rendered PNG directly
over HTTP, so Drive is not in the loop at all.

Writes raw maps plus manifest.json into output/. build_deck.py reads that
manifest, so you can redo the slides without re-running this.
"""

import calendar
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import ee
import requests
import yaml
from dotenv import load_dotenv

from bands import BAND_SPECS, rows_for, panel_title


def initialize():
    """Connect to Earth Engine.

    Uses a service account when one is configured, otherwise your personal
    login. The service account is what you want eventually, because it
    belongs to the lab rather than to a person.
    """
    load_dotenv()

    project = os.environ.get("EE_PROJECT")
    if not project:
        raise SystemExit(
            "EE_PROJECT is not set. Copy .env.example to .env and fill it in."
        )

    key_file = os.environ.get("EE_SERVICE_ACCOUNT_KEY")
    sa_email = os.environ.get("EE_SERVICE_ACCOUNT_EMAIL")

    if key_file and sa_email:
        ee.Initialize(ee.ServiceAccountCredentials(sa_email, key_file),
                      project=project)
        print(f"Earth Engine ready (service account, project {project})")
    else:
        ee.Initialize(project=project)
        print(f"Earth Engine ready (personal login, project {project})")


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def get_collection(spec, band):
    """One or several collection ids, merged, with the band selected.

    SMAP needs this: the record is split across version 005 and 006.
    """
    ids = spec if isinstance(spec, list) else [spec]
    coll = ee.ImageCollection(ids[0]).select(band)
    for extra in ids[1:]:
        coll = coll.merge(ee.ImageCollection(extra).select(band))
    return coll


def utc_date(millis):
    """An Earth Engine timestamp as a UTC calendar date, as a string."""
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date().isoformat()


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def latest_complete_month(limit):
    """The most recent calendar month that `limit` fully covers.

    Data through 31 August means August is reportable. Data through
    15 September does not, because half the month is missing, so the
    answer is still August.
    """
    last_day = calendar.monthrange(limit.year, limit.month)[1]
    if limit.day >= last_day:
        return limit.year, limit.month
    month, year = limit.month - 1, limit.year
    if month < 1:
        month, year = 12, year - 1
    return year, month


def resolve_month(config, latest_by_key):
    """Which month this bulletin reports on.

    Normally worked out from what Earth Engine actually holds: the dataset
    that lags the most decides, because every slide has to cover the same
    month. CHIRPS is usually the laggard, by about four weeks.

    Setting year and month in the config overrides this, which is how you
    rebuild an older bulletin.
    """
    bulletin = config["bulletin"]
    if bulletin.get("year") and bulletin.get("month"):
        year, month = bulletin["year"], bulletin["month"]
        print(f"\nReporting month (from config): {MONTHS[month - 1]} {year}")
        return year, month

    limiting = min(latest_by_key, key=lambda k: latest_by_key[k])
    limit = date.fromisoformat(latest_by_key[limiting])
    year, month = latest_complete_month(limit)

    label = next(layer["label"] for layer in config["layers"]
                 if layer["key"] == limiting)
    print(f"\nReporting month: {MONTHS[month - 1]} {year}")
    print(f"  limited by {label}, which has data through {limit}")
    return year, month


def window_start(year, month, days):
    """Start of a trailing window ending with the reported month.

    A 3-month figure covers the reported month and the two before it.
    """
    months_back = max(0, (days // 30) - 1)
    m, y = month - months_back, year
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


def anomaly_image(coll, band, start, interval_days, clim_start, clim_end,
                  want_vci=False):
    """Standardized anomaly for one variable over one window.

    The same calculation serves rainfall, soil moisture and vegetation,
    which is why the monitoring scripts are near-copies of each other.

        deviation    = this window's mean - the historical mean for this slot
        standardized = deviation / historical standard deviation
        anomaly_pct  = deviation / historical mean * 100

    The spread in the denominator is the year-to-year spread of interval
    means, not the spread of individual daily observations.
    """
    clim_years = ee.List.sequence(clim_start, clim_end)

    # Which slot of the calendar this window falls in. Slots step by
    # interval_days from Jan 1, the same keying the GEE scripts use.
    doy = start.getRelative("day", "year")
    slot = doy.divide(interval_days).round().multiply(interval_days)
    slot_ref = ee.Date("2018-01-01").advance(slot, "day")
    slot_from = slot_ref.getRelative("day", "year")
    slot_to = slot_ref.advance(interval_days, "day").getRelative("day", "year")

    def one_year(y):
        return (coll
                .filter(ee.Filter.calendarRange(y, y, "year"))
                .filter(ee.Filter.calendarRange(slot_from, slot_to, "day_of_year"))
                .mean()
                .set("year", y))

    per_year = ee.ImageCollection.fromImages(clim_years.map(one_year))

    hist_mean = per_year.mean().rename(band)
    hist_sd = per_year.reduce(ee.Reducer.stdDev()).rename(band)

    current = (coll.filterDate(start, start.advance(interval_days, "day"))
               .mean().rename(band))

    deviation = current.subtract(hist_mean)

    out = (deviation.divide(hist_sd).rename("standardized")
           .addBands(deviation.divide(hist_mean).multiply(100)
                     .rename("anomaly_pct")))

    if want_vci:
        # Where this window sits between the worst and best the baseline
        # has seen. 0 = worst on record, 100 = best.
        lo = per_year.min().rename(band)
        hi = per_year.max().rename(band)
        out = out.addBands(current.subtract(lo).divide(hi.subtract(lo))
                           .multiply(100).rename("vci"))

    return out


def download_png(image, vis, region, width, out_path):
    """Render one band and save it as a PNG.

    getThumbURL returns a rendered image over HTTP. Faster than a Drive
    export and there is nothing to fetch by hand afterwards. The tradeoff
    is a size ceiling, which a country-scale map stays well under.
    """
    url = image.visualize(**vis).getThumbURL({
        "region": region,
        "dimensions": width,
        "format": "png",
    })
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    out_path.write_bytes(response.content)


def main():
    initialize()
    config = load_config()

    region_cfg = config["region"]
    features = (ee.FeatureCollection(region_cfg["boundary_collection"])
                .filter(ee.Filter.eq(region_cfg["filter_property"],
                                     region_cfg["filter_value"])))
    geom = features.geometry().bounds()

    out_dir = Path(config["output"]["dir"])
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    width = config["output"]["map_width_px"]

    prefix = region_cfg["filter_value"].lower()

    # Ask Earth Engine what it actually holds before deciding anything.
    # The answer sets both the reporting month and the "data through"
    # line on each slide.
    print("\nChecking data availability ...")
    collections, latest_by_key = {}, {}
    for layer in config["layers"]:
        coll = get_collection(layer["collection"], layer["band"])
        collections[layer["key"]] = coll

        span = coll.reduceColumns(
            ee.Reducer.minMax(), ["system:time_start"]).getInfo()
        # Earth Engine timestamps are UTC. Converting them with the local
        # clock shifts an image stamped at UTC midnight back a day, which
        # is enough to make a finished month look incomplete.
        earliest = utc_date(span["min"])
        latest = utc_date(span["max"])
        latest_by_key[layer["key"]] = latest

        print(f"  {layer['label']}: {earliest} to {latest}")

        # The method slide states a climatology period. Check the data can
        # actually back that claim, because a baseline quietly computed from
        # two years instead of ten produces anomalies that look fine and
        # are wrong.
        if int(earliest[:4]) > layer["clim_start"]:
            print(f"    WARNING: config claims a climatology from "
                  f"{layer['clim_start']}, but this collection only starts "
                  f"{earliest}.")
            print(f"    Either widen the collection list or correct "
                  f"clim_start, or the method slide will overstate it.")

    year, month = resolve_month(config, latest_by_key)

    slides = []

    for layer in config["layers"]:
        coll = collections[layer["key"]]
        latest = latest_by_key[layer["key"]]
        print(f"\n{layer['label']}")

        panels = []
        for band in rows_for(layer):
            for window in config["windows"]:
                start = window_start(year, month, window["days"])
                ee_start = ee.Date.fromYMD(start.year, start.month, 1)

                image = anomaly_image(
                    coll, layer["band"], ee_start, window["days"],
                    layer["clim_start"], layer["clim_end"],
                    want_vci=(band == "vci"),
                )

                spec = BAND_SPECS[band]
                filename = f"{prefix}_{layer['key']}_{band}_{window['key']}.png"
                print(f"  {band} {window['label']} ...")
                download_png(
                    image.select(band),
                    {"min": spec["min"], "max": spec["max"],
                     "palette": layer["palette"]},
                    geom, width, raw_dir / filename,
                )

                panels.append({
                    "band": band,
                    "window": window["key"],
                    "raw_image": filename,
                    "title": panel_title(band, layer["label"], window["label"]),
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
            "data_through": latest,
            "panels": panels,
        })

    manifest = {
        "region": region_cfg["name"],
        "title": config["bulletin"]["title"],
        "year": year,
        "month": month,
        "generated": date.today().isoformat(),
        "slides": slides,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote maps to {raw_dir}/ and manifest.json to {out_dir}/")
    print("Next: python build_deck.py")


if __name__ == "__main__":
    main()
