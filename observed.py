"""
Pull observed rainfall from CHIRPS, shaped for the forecast slide.

This mirrors what the Code Editor dashboard does, so the deck and the
dashboard answer with the same numbers. Three choices here come straight
from that script rather than from what would be natural in Python:

  the dataset      CHIRPS version 3 pentads, not version 2 dailies. A
                   pentad is a five-day total, so a season is eighteen
                   points rather than ninety-two. The dashboard's
                   climatology and drought threshold both come from this.

  the current year a separate, preliminary product, which runs a couple of
                   days behind instead of about twenty-five. That is the
                   only way the dashboard can show a season that is still
                   running.

  the grid         rainfall resolution, not the forecast's. The forecast is
                   a 1 degree grid; averaging rainfall down to it before
                   taking a percentile throws away most of the variation
                   the threshold is supposed to capture. The dashboard
                   leaves rainfall where it is and lets the coarse forecast
                   be the coarse part.

Outputs:

  forecasts/observed_seasonal.tif
      One band per historical year, each the season total. The drought
      threshold is a percentile of these.

  forecasts/region_mask.tif
      The country on the same grid. PyCPT output is a plain rectangle and
      for Yemen that rectangle also holds Saudi Arabia, Oman, the Red Sea
      and part of the Horn of Africa.

  forecasts/region_masks.tif
      The same again per admin unit, one band each, so a chart can be
      drawn for a single governorate.

  forecasts/regions.geojson
      The admin unit borders, simplified, for outlining a selected
      region on the forecast maps.

  output/observed.json
      Cumulative rainfall averaged over the country, pentad by pentad:
      this season, and the same curve for every past season. The chart
      draws the first; the conditional probability is worked out from
      both, by finding past years that had about this much rain by this
      point. Also holds each admin unit's own curve and its rainfall by
      this point in every past year.

    python observed.py

Needs Earth Engine, same credentials as fetch.py.
"""

import argparse
import json
import math
import time
from datetime import date, timedelta
from pathlib import Path

import ee
import rasterio
import requests
import yaml

from fetch import initialize, utc_date
from settings import config_argument, load_config


# The dashboard's climatology and threshold come from this one.
CHIRPS = "UCSB-CHC/CHIRPS/V3/PENTAD"

# And the season in progress from this one. The final product is about
# twenty-five days behind, which would leave the last three weeks of any
# running season invisible. This is behind by about two.
CHIRPS_CURRENT = "projects/climate-engine-pro/assets/ce-chirps-prelim-pentad"


def grid_from_bounds(west, south, east, north, cell):
    """The smallest grid of whole rainfall cells that covers a box.

    Each edge is pushed outward to the next multiple of the cell size,
    so nothing of the country is cut off and the grid lands exactly on
    the rainfall product's own pixels. That last part matters: CHIRPS
    cells sit on multiples of 0.05 degrees, so a grid snapped this way
    reads each rainfall pixel as it is, with no resampling in between.

    Worked through for a box from 42.53 to 54.48 east at 0.05 degrees:
    42.53 moves out to 42.50, 54.48 moves out to 54.50, and the width is
    (54.50 - 42.50) / 0.05 = 240 cells.

    The tiny tolerance is for edges already on a multiple, which
    floating point would otherwise push a whole cell further out.
    """
    tol = 1e-9
    left = math.floor(west / cell + tol) * cell
    right = math.ceil(east / cell - tol) * cell
    bottom = math.floor(south / cell + tol) * cell
    top = math.ceil(north / cell - tol) * cell
    return {
        "bounds": [round(left, 6), round(bottom, 6),
                   round(right, 6), round(top, 6)],
        "width": int(round((right - left) / cell)),
        "height": int(round((top - bottom) / cell)),
    }


def computed_grid(region):
    """Work the analysis grid out, so nobody has to type it.

    Two facts are needed and both are already to hand: the country's
    bounding box, from the same boundaries everything is clipped with,
    and the size of a rainfall cell, from the rainfall product itself.
    """
    ring = region.bounds().coordinates().get(0).getInfo()
    lons = [point[0] for point in ring]
    lats = [point[1] for point in ring]

    transform = (ee.ImageCollection(CHIRPS).first()
                 .projection().getInfo().get("transform") or [])
    # Rounded, because the product stores its cell size as a 32-bit
    # number, 0.0500000007..., and using that as it stands puts the
    # edges at values like 41.800001 instead of 41.8.
    cell = round(abs(transform[0]), 6) if transform else 0.05
    return grid_from_bounds(min(lons), min(lats), max(lons), max(lats),
                            cell), cell


def season_spec(spec):
    """Season start and length in days, the way the dashboard states it.

    It is given there as a start day of the year and a run of days, so
    that is what is accepted here. season_months is still honoured as a
    fallback, since every other region in this config is written that way.
    """
    start_day = spec.get("season_start_day")
    length = spec.get("season_length_days")
    if start_day and length:
        return int(start_day), int(length)

    months = spec.get("season_months")
    if not months:
        raise SystemExit(
            "Set either forecasts.season_start_day and "
            "forecasts.season_length_days,\n"
            "or forecasts.season_months, in the config."
        )
    start_month, end_month = months
    start = date(2001, start_month, 1)
    last_day = 31 if end_month in (1, 3, 5, 7, 8, 10, 12) else (
        30 if end_month != 2 else 28)
    end = date(2001, end_month, last_day)
    wrap = 365 if end < start else 0
    return start.timetuple().tm_yday, (end - start).days + 1 + wrap


def season_window(year, start_day, length):
    """The season's date range for one year, as Earth Engine dates.

    Half open, [start, end), which is what filterDate wants and what the
    dashboard uses. With pentads that quietly includes the pentad that
    straddles the end, since a pentad is stamped at its first day.
    """
    start = ee.Date.fromYMD(year, 1, 1).advance(start_day - 1, "day")
    return start, start.advance(length, "day")


def season_dates(year, start_day, length):
    """The same window as plain Python dates, for printing and arithmetic."""
    start = date(year, 1, 1) + timedelta(days=start_day - 1)
    return start, start + timedelta(days=length - 1)


def seasonal_totals(years, start_day, length):
    """One band per year, each that season's total rainfall.

    Bands are named by year. Nothing downstream reads the names, since
    everything works by band order, but a file that says which year each
    band is can be opened by someone else and understood.
    """
    chirps = ee.ImageCollection(CHIRPS).select("precipitation")

    def one_year(year):
        start, end = season_window(year, start_day, length)
        return (chirps.filterDate(start, end)
                .sum()
                .rename(ee.String("y").cat(ee.Number(year).format("%d")))
                .set("year", year))

    return [one_year(y) for y in years]


def label_bands(path, names):
    """Write year labels into a downloaded GeoTIFF.

    Earth Engine's download drops band names and hands back b1, b2, b3,
    so the years have to be put back afterwards.
    """
    with rasterio.open(path, "r+") as dst:
        for i, name in enumerate(names, start=1):
            dst.set_band_description(i, str(name))


def download_geotiff(image, region, width, height, out_path):
    """Save a multi-band image as a GeoTIFF on an exact pixel grid.

    Asking for explicit dimensions over an explicit region is what pins
    the result to a known grid, so forecast.py can line it up against the
    forecast without guessing at a half-pixel offset.
    """
    url = image.getDownloadURL({
        "region": region,
        "dimensions": f"{width}x{height}",
        "format": "GEO_TIFF",
    })
    response = requests.get(url, timeout=600)
    response.raise_for_status()
    out_path.write_bytes(response.content)


def pentad_means(collection, region, start, end, scale):
    """Rainfall averaged over the region, one value per pentad.

    Returned as [(date, mm), ...]. One reduceRegion per pentad would be
    dozens of round trips, so the whole range is reduced server side and
    pulled back in a single call.
    """
    images = collection.filterDate(start, end)

    def mean_for(image):
        value = image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=scale,
            maxPixels=1e9,
        ).get("precipitation")
        return ee.Feature(None, {
            "date": image.date().format("YYYY-MM-dd"),
            "mm": value,
        })

    rows = with_retries(lambda: images.map(mean_for).getInfo())["features"]
    rows = [(r["properties"]["date"], r["properties"].get("mm"))
            for r in rows]
    return sorted(rows, key=lambda r: r[0])


def cumulative(series):
    """Running total, skipping pentads Earth Engine had no value for."""
    total, out = 0.0, []
    for day, mm in series:
        if mm is not None:
            total += mm
        out.append((day, round(total, 3)))
    return out


def historical_curves(region, years, start_day, length, scale):
    """Each past season's cumulative rainfall, pentad by pentad.

    Two jobs. It gives the chart something to show, and it is what the
    conditional probability is built from: to find the past years that
    had about this much rain by this point in the season, you need every
    past year's running total at that point.

    All years are reduced server side in one call.
    """
    chirps = ee.ImageCollection(CHIRPS).select("precipitation")

    def one_year(year):
        start, end = season_window(year, start_day, length)

        def mean_for(image):
            value = image.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=region,
                scale=scale, maxPixels=1e9).get("precipitation")
            return ee.Feature(None, {"mm": value})

        season = chirps.filterDate(start, end).sort("system:time_start")
        return ee.Feature(None, {
            "year": year,
            "pentads": season.map(mean_for).aggregate_array("mm"),
        })

    rows = with_retries(lambda: ee.FeatureCollection(
        [one_year(y) for y in years]).getInfo())["features"]

    curves = {}
    for row in rows:
        props = row["properties"]
        total, running = 0.0, []
        for mm in props.get("pentads") or []:
            if mm is not None:
                total += mm
            running.append(round(total, 3))
        if running:
            curves[props["year"]] = running
    return curves


def with_retries(action, tries=4, pause=20):
    """Run something against Earth Engine, waiting out any throttling.

    Earth Engine caps how many aggregations one project may have in
    flight, and answers a burst with "Too many concurrent aggregations"
    rather than queueing. The work below is built to ask in as few
    aggregations as possible, but a shared project can still be busy
    with someone else's run, so a refusal is worth waiting out rather
    than failing on.
    """
    for attempt in range(1, tries + 1):
        try:
            return action()
        except ee.EEException as error:
            throttled = any(phrase in str(error).lower() for phrase in
                            ("concurrent aggregation", "too many",
                             "quota", "rate limit"))
            if not throttled or attempt == tries:
                raise
            wait = pause * attempt
            print(f"  Earth Engine is busy ({error}).")
            print(f"  Waiting {wait}s, then trying again "
                  f"({attempt} of {tries - 1}) ...")
            time.sleep(wait)


def admin_units(region_cfg):
    """The admin units inside the region, as an Earth Engine collection.

    The same boundaries the maps are clipped with, so a regional chart
    and the national one are cut from one source. Their names and ids are
    printed by build_deck.py --list-regions.
    """
    return (ee.FeatureCollection(region_cfg["boundary_collection"])
            .filter(ee.Filter.eq(region_cfg["filter_property"],
                                 region_cfg["filter_value"])))


def region_mask_stack(units, ids, bounds, width, height, out_path):
    """One band per admin unit, each marking where that unit is.

    Downloaded once as a single file rather than one per unit, because
    the regional charts are only a question of which pixels to count and
    the rasters they count are already on disk.
    """
    bands = [ee.Image.constant(1).toFloat()
             .clip(units.filter(ee.Filter.eq("shapeID", uid)))
             .rename(f"u{i}")
             for i, uid in enumerate(ids)]
    download_geotiff(ee.Image.cat(bands), bounds, width, height, out_path)
    label_bands(out_path, ids)


def region_outlines(units, out_path, tolerance_m=1000):
    """The admin unit borders as GeoJSON, for drawing on the maps.

    Simplified before download rather than after. The full polygons are
    tens of megabytes of coastline detail, none of which a map this size
    can show: at 0.05 degree cells, a kilometre is a fifth of a pixel.
    What is wanted is a line a reader can see, not a faithful coastline.

    Vector rather than tracing the mask raster, because a mask traced at
    0.05 degrees comes out as a visible staircase once it is drawn onto
    an eight inch figure.
    """
    simple = units.map(lambda f: ee.Feature(
        f.geometry().simplify(tolerance_m),
        {"id": f.get("shapeID"), "name": f.get("shapeName")}))
    out_path.write_text(json.dumps(with_retries(simple.getInfo)))


def reduce_over_units(stack, units, scale):
    """Average a multi-band image over every unit, in one aggregation.

    This is the whole trick for staying inside Earth Engine's limits.
    One reduceRegions over an image with forty bands is a single
    aggregation; forty reduceRegions over one-band images is forty at
    once, and Earth Engine refuses that outright rather than queueing.

    Geometry is dropped on the way back, since the answer is a few
    numbers per unit and the outlines would be most of the download.
    """
    rows = with_retries(
        lambda: stack.reduceRegions(collection=units,
                                    reducer=ee.Reducer.mean(),
                                    scale=scale)
        .map(lambda f: ee.Feature(None, f.toDictionary()))
        .getInfo())
    return [r["properties"] for r in rows["features"]]


def regional_current(collection, units, start, end, scale):
    """This season's rainfall per unit, per pentad.

    The pentads are stacked into one image with a band each, named by
    position so the bands can be matched back to their dates without
    trusting Earth Engine's own naming.
    """
    images = collection.filterDate(start, end).sort("system:time_start")
    stamps = images.aggregate_array("system:time_start").getInfo()
    ids = images.aggregate_array("system:index").getInfo()
    if not stamps:
        return [], []

    stack = ee.Image.cat([
        images.filter(ee.Filter.eq("system:index", index)).first()
        .rename(f"p{position}")
        for position, index in enumerate(ids)])

    rows = reduce_over_units(stack, units, scale)
    dates = [utc_date(stamp) for stamp in stamps]

    out = []
    for props in rows:
        uid = props.get("shapeID")
        for position, day in enumerate(dates):
            out.append({"id": uid, "date": day,
                        "mm": props.get(f"p{position}")})
    return out, dates


def regional_to_date(units, years, start_day, days, scale):
    """Each past season's rainfall per unit, up to the same point.

    days is how far into the season to count, measured from its first
    day. One extra day is added to the range because a pentad is stamped
    at its first day and filterDate's end is exclusive, so without it the
    pentad landing exactly on the cut off would be dropped.

    One band per year, so all of it is one aggregation.
    """
    chirps = ee.ImageCollection(CHIRPS).select("precipitation")

    def one_year(year):
        start = ee.Date.fromYMD(year, 1, 1).advance(start_day - 1, "day")
        return (chirps.filterDate(start, start.advance(days + 1, "day"))
                .sum().rename(f"y{year}"))

    stack = ee.Image.cat([one_year(y) for y in years])
    rows = reduce_over_units(stack, units, scale)

    out = []
    for props in rows:
        uid = props.get("shapeID")
        for year in years:
            out.append({"id": uid, "year": year,
                        "mm": props.get(f"y{year}")})
    return out


def latest_date(collection):
    """The most recent timestamp in a collection, as a date."""
    span = (collection
            .reduceColumns(ee.Reducer.max(), ["system:time_start"])
            .getInfo())
    return date.fromisoformat(utc_date(span["max"]))


def main(argv=None):
    args = config_argument(argparse.ArgumentParser(
        description="Pull the observed rainfall record from Earth Engine."
    )).parse_args(argv)

    initialize()
    base_dir = Path(__file__).parent
    config = load_config(args.config)

    spec = config.get("forecasts") or {}
    region_cfg = config["region"]
    features = (ee.FeatureCollection(region_cfg["boundary_collection"])
                .filter(ee.Filter.eq(region_cfg["filter_property"],
                                     region_cfg["filter_value"])))
    region = features.geometry()

    # Everything is computed on this grid, at rainfall resolution rather
    # than the forecast's. See the note at the top. It is worked out from
    # the country outline unless the config pins one, which is worth
    # doing only to trim the box or to freeze it against a change in the
    # boundary data.
    grid = spec.get("analysis_grid")
    if grid:
        print(f"Analysis grid from the config: {grid['width']} x "
              f"{grid['height']} cells over {grid['bounds']}")
    else:
        grid, cell = computed_grid(region)
        print(f"Analysis grid worked out from the {region_cfg['name']} "
              f"outline: {grid['width']} x {grid['height']} cells of "
              f"{cell:g} degrees\n  over {grid['bounds']}")
    bounds = ee.Geometry.Rectangle(grid["bounds"], None, False)

    start_day, length = season_spec(spec)
    print(f"Season: day {start_day} of the year, {length} days long")

    chirps = ee.ImageCollection(CHIRPS).select("precipitation")
    latest = latest_date(chirps)
    print(f"CHIRPS v3 pentads run through {latest}, "
          f"{(date.today() - latest).days} days behind today")

    # The season in progress comes from the preliminary product, which is
    # far less far behind. If this account cannot read it, fall back to
    # the final product and say so rather than failing the run.
    try:
        current_coll = (ee.ImageCollection(CHIRPS_CURRENT)
                        .select("precipitation"))
        current_latest = latest_date(current_coll)
        print(f"Preliminary pentads run through {current_latest}, "
              f"{(date.today() - current_latest).days} days behind")
    except Exception as error:
        current_coll, current_latest = chirps, latest
        print(f"\nCannot read the preliminary rainfall product, so the "
              f"season in progress\n  comes from the final one instead and "
              f"stops about three weeks earlier.\n  ({error})")

    # `or` rather than a get() default: a key left blank in the YAML is
    # present and None, not missing, so get()'s default never fires.
    clim_start = spec.get("observed_start") or 1983
    clim_end = spec.get("observed_end") or (latest.year - 1)
    years = list(range(clim_start, clim_end + 1))
    print(f"Historical years: {years[0]} to {years[-1]} ({len(years)} of them)")

    this_year = spec.get("season_year") or current_latest.year
    this_year = int(this_year)
    season_start, season_end = season_dates(this_year, start_day, length)
    print(f"Monitoring {this_year}: {season_start} to {season_end}")

    if this_year in years:
        print(f"  {this_year} is inside the historical range, so it is "
              f"dropped from the\n  comparison to avoid measuring the "
              f"season against itself.")
        years = [y for y in years if y != this_year]

    # ---- per-pixel seasonal totals, for the threshold ----
    out_dir = base_dir / spec.get("dir", "forecasts")
    out_dir.mkdir(parents=True, exist_ok=True)

    mask_path = out_dir / "region_mask.tif"
    print(f"\nBuilding the {region_cfg['name']} mask on the "
          f"{grid['width']}x{grid['height']} grid ...")
    download_geotiff(ee.Image.constant(1).toFloat().clip(region),
                     bounds, grid["width"], grid["height"], mask_path)
    with rasterio.open(mask_path) as src:
        inside = src.read(1, masked=True).count()
        total = src.read(1).size
    print(f"  {inside} of {total} cells are inside {region_cfg['name']} "
          f"({inside / total * 100:.0f}% of the rectangle)")

    tif_path = out_dir / "observed_seasonal.tif"
    print(f"\nBuilding {len(years)} seasonal totals on the same grid ...")
    stack = ee.Image.cat(seasonal_totals(years, start_day, length))
    download_geotiff(stack, bounds, grid["width"], grid["height"], tif_path)
    label_bands(tif_path, years)
    print(f"  wrote {tif_path}")

    # ---- country-average curves, for the chart and the analog years ----
    # CHIRPS is 0.05 degrees; asking for its native scale in metres keeps
    # the average honest.
    scale = 5566

    print(f"\nPulling {len(years)} historical curves. This is the slow "
          f"part ...")
    curves = historical_curves(region, years, start_day, length, scale)
    lengths = [len(c) for c in curves.values()]
    print(f"  {len(curves)} years, {min(lengths)} to {max(lengths)} "
          f"pentads each")
    finals = sorted(c[-1] for c in curves.values())
    mid = finals[len(finals) // 2]
    print(f"  a normal season totals about {mid:.0f} mm, "
          f"ranging {finals[0]:.0f} to {finals[-1]:.0f} mm")

    print(f"\nPulling {this_year}'s rainfall so far ...")
    start_ee, end_ee = season_window(this_year, start_day, length)
    current = cumulative(pentad_means(current_coll, region, start_ee, end_ee,
                                      scale))
    if current:
        through = date.fromisoformat(current[-1][0])
        elapsed = (through - season_start).days
        print(f"  {len(current)} pentads, {current[-1][1]:.1f} mm through "
              f"{through} ({elapsed} days into the season)")
        if through >= season_end - timedelta(days=5):
            print(f"  the season is complete, so this is the whole of it")
    else:
        elapsed = 0
        print(f"  nothing yet. The season starts {season_start} and the "
              f"record only reaches {current_latest}.")

    # ---- the same again per admin unit, for the regional charts ----
    #
    # Pulled for every unit rather than for a list someone has to choose
    # up front, so build_deck.py --regions can ask for any of them
    # without this having to run again. It is cheap: the flat lines and
    # the histogram come from rasters already on disk, so all that is
    # needed from Earth Engine is each unit's rainfall curve and its
    # rainfall by this point in each past year.
    regions, analog_index, analog_cut = {}, None, None
    if spec.get("regional_data", True) and current:
        units = admin_units(region_cfg)
        listing = units.map(lambda f: ee.Feature(None, {
            "id": f.get("shapeID"), "name": f.get("shapeName"),
        })).getInfo()["features"]
        pairs = sorted((f["properties"]["id"],
                        f["properties"].get("name") or "unnamed")
                       for f in listing)
        ids = [i for i, _ in pairs]
        names = dict(pairs)
        print(f"\n{len(ids)} admin units in {region_cfg['name']}. "
              f"Pulling what the regional charts need ...")

        masks_path = out_dir / "region_masks.tif"
        region_mask_stack(units, ids, bounds, grid["width"], grid["height"],
                          masks_path)
        print(f"  wrote {masks_path}  (one band per unit)")

        shapes_path = out_dir / "regions.geojson"
        region_outlines(units, shapes_path)
        print(f"  wrote {shapes_path}  "
              f"({shapes_path.stat().st_size / 1024:.0f} KB of borders)")

        # Where in the season the past years get compared. Same rule as
        # the national chart, including the dashboard's one pentad step
        # back, so a regional answer and the national one are built the
        # same way.
        offset = int(spec.get("analog_offset_pentads", 1) or 0)
        analog_index = max(0, len(current) - 1 - offset)
        analog_cut = date.fromisoformat(current[analog_index][0])
        days_in = (analog_cut - season_start).days
        print(f"  comparing past years up to {analog_cut}, "
              f"{days_in} days into the season")

        now_rows, _ = regional_current(current_coll, units, start_ee,
                                       end_ee, scale)
        past_rows = regional_to_date(units, years, start_day, days_in, scale)

        # Named apart from the national `curves` above on purpose. These
        # were once both called the same thing, and this block quietly
        # overwrote the national curves before they were written out, so
        # observed.json came back keyed by governorate id where the
        # charts expected years.
        unit_curves, unit_totals = {}, {}
        for row in now_rows:
            unit_curves.setdefault(row["id"], []).append(
                (row["date"], row["mm"]))
        for row in past_rows:
            unit_totals.setdefault(row["id"], {})[str(row["year"])] = \
                row["mm"]

        for uid in ids:
            series = sorted(unit_curves.get(uid, []), key=lambda r: r[0])
            regions[uid] = {
                "name": names.get(uid, uid),
                "current_cumulative": cumulative(series),
                "to_date_by_year": {
                    year: (round(value, 3) if value is not None else None)
                    for year, value in (unit_totals.get(uid) or {}).items()},
            }
        wettest = max(regions.values(),
                      key=lambda r: (r["current_cumulative"] or [("", 0)])[-1][1])
        print(f"  {len(regions)} units done. Wettest so far is "
              f"{wettest['name']} at "
              f"{wettest['current_cumulative'][-1][1]:.0f} mm")

    record = {
        "region": region_cfg["name"],
        "season_start_day": start_day,
        "season_length": length,
        "season_start": season_start.isoformat(),
        "season_end": season_end.isoformat(),
        "chirps_through": current_latest.isoformat(),
        "lag_days": (date.today() - current_latest).days,
        "historical_years": years,
        "current_year": this_year,
        "current_cumulative": current,
        # One cumulative curve per past season, pentad by pentad, each
        # starting at the season's first pentad. Keyed by year as a
        # string, because JSON has no integer keys.
        "historical_curves": {str(y): c for y, c in curves.items()},
        "days_elapsed": elapsed,
        "in_season": bool(current),
        # The grid every raster here is on, whether typed or worked out.
        "analysis_grid": grid,
        # Per admin unit, for build_deck.py --regions. Keyed by the same
        # id that build_deck.py --list-regions prints.
        "regions": regions,
        "analog_index": analog_index,
        "analog_cut": analog_cut.isoformat() if analog_cut else None,
        "generated": date.today().isoformat(),
    }
    out = base_dir / config["output"]["dir"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "observed.json").write_text(json.dumps(record, indent=2))
    print(f"  wrote {out / 'observed.json'}")

    # Only speak up if the config is not already pointing at what was
    # just written. When it is, there is nothing for anyone to do.
    folder = spec.get("dir", "forecasts")
    wanted = {"region_mask": f"{folder}/region_mask.tif",
              "observed_rainfall": f"{folder}/observed_seasonal.tif"}
    unset = {k: v for k, v in wanted.items() if spec.get(k) != v}
    if unset:
        print(f"\nSet these in {args.config} so forecast.py picks them up:")
        for key, value in unset.items():
            print(f"  {key}: {value}")
    print(f"\nNext: python build_deck.py --config {args.config}")


if __name__ == "__main__":
    main()