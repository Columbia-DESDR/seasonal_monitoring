# Yemen bulletin generator

Builds the seasonal monitoring bulletin as a PowerPoint deck, in the same
shape as the one Nitin assembled by hand for OCHA: a title page, one slide
per variable with a 2x2 grid of maps, and a sources and method page.

Two steps, deliberately separate:

```
fetch.py       Earth Engine  ->  maps + manifest.json in output/
build_deck.py  manifest.json ->  .pptx (and .pdf with --pdf)
```

Keeping them apart means you can redo the slides as often as you like
without re-hitting Earth Engine, which is the slow part.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then fill in EE_PROJECT
```

## Try the deck half first

Earth Engine authentication is the fiddly part, so you can skip it to start:

```bash
python make_placeholders.py       # fake maps, no Earth Engine
python build_deck.py
```

That proves the layout works before you fight with auth.

## Then the real thing

One-time Earth Engine authentication:

```bash
python -c "import ee; ee.Authenticate()"
```

Your Google Cloud project also needs to be registered for Earth Engine with
the API enabled. Put the project id in `.env` as `EE_PROJECT`.

```bash
rm -rf output                     # clear the placeholders
python fetch.py
python build_deck.py --pdf
```

## What each slide holds

Every variable gets one slide, laid out as a 2x2 grid:

|  | 1-month | 3-month |
|---|---|---|
| **top row** | standardized anomaly | standardized anomaly |
| **bottom row** | % departure from normal | % departure from normal |

Vegetation swaps the bottom row for VCI, which is what the existing
bulletin does. That choice is the `second_row` key in the config.

Windows are trailing, so the 3-month figure covers the reported month and
the two before it.

## Changing what gets built

Everything lives in `config.yaml`: which country, which month, and the three
variables with their datasets, baseline years, palettes and bottom row.

Pointing this at another country should mean editing that file and nothing
else. That is the main thing worth testing once Yemen works.

## Which month it reports on

Not configured. `fetch.py` asks Earth Engine what each dataset actually
holds, and reports on the most recent month that **all** of them fully
cover. Every slide has to describe the same month, so the dataset that lags
most decides, and that is normally CHIRPS, which runs about four weeks
behind.

A month only counts as covered when the data reaches its last day. Data
through 29 September still means an August bulletin, because September is
incomplete.

It prints what it chose and why:

```
Checking data availability ...
  Rainfall: data through 2026-08-31
  Soil Moisture: data through 2026-09-29
  Vegetation: data through 2026-09-06

Reporting month: August 2026
  limited by Rainfall, which has data through 2026-08-31
```

To rebuild an older bulletin, set both `bulletin.year` and `bulletin.month`
in the config. That overrides the automatic choice. Leave them blank for
normal runs.

The "data through" line on each slide is the newest data in that dataset,
not the end of the window shown. Worth confirming with Max that this is the
reading he wants, since the existing bulletin is ambiguous about it.

## The forecast slide

The existing bulletin opens with PyCPT forecast maps from the maproom, which
is not Earth Engine, so they cannot be pulled the same way. Drop the
screenshots into `forecasts/`, list them under `forecasts.images` in the
config, and the slide gets built. Leave the list empty and the slide is
skipped.

Automating that one properly is a separate question for Max.

## Files

| File | What it does |
|---|---|
| `config.yaml` | Region, month, variables, boilerplate text. The only file you should normally edit. |
| `fetch.py` | Talks to Earth Engine, computes anomalies, saves the maps. |
| `build_deck.py` | Adds titles and colour bars, then assembles the deck. |
| `bands.py` | Titles and value ranges for each kind of panel. |
| `make_placeholders.py` | Fake data for working on layout offline. |

## How this relates to the GEE scripts

The `yemen_*_monitoring` scripts in `users/maxmauerman/wfp_yemen` each compute
a standardized anomaly and export rendered images to the Drive folder
`yemen_bulletin_exports`. `fetch.py` does the same calculation through the
Python API and pulls the rendered map directly over HTTP, so Drive is not
involved.

There are six of those scripts because the calculation is repeated per
variable and per window length. Here it is one function, with the dataset,
baseline years and window length as arguments.

In the existing deck the panel titles and colour bars are baked into the
picture rather than being slide text. `build_deck.py` draws them the same
way, with matplotlib, so the panels drop into the same slots.

## Open questions for Max

- The GEE scripts build the historical mean from every available year,
  including the one being monitored, while the standard deviation uses an
  explicit baseline list. This code uses the same baseline years for both,
  so numbers will differ slightly from the Code Editor output. Worth
  confirming which is intended before anyone compares the two.
- `yemen_seasonal_forecast_monitoring` has a typo, `empty.painat(...)` where
  it should be `paint`, which stops the script running.
- The page numbers in the existing deck repeat 2 twice. Fixed here.
- Should the deck land somewhere shared rather than a local folder, and
  should this run on a schedule under a service account?

## Security

`.env`, any `*.json` credentials and the `output/` folder are gitignored.
Never commit a service account key. If one is ever pushed, rotate it rather
than deleting the file, because git keeps history.

## Converting PyCPT forecasts to GeoTIFF

`pycpt_to_geotiff.py` turns PyCPT NetCDF output into a multiband GeoTIFF
(one band per year, band names are the years) for ingestion into Earth Engine.

    python pycpt_to_geotiff.py mu  MME_deterministic_hindcasts.nc output/hindcast_mu.tif
    python pycpt_to_geotiff.py var MME_hindcast_prediction_error_variance.nc output/hindcast_var.tif
    python pycpt_to_geotiff.py mu  MME_deterministic_forecast_2026.nc output/forecast_mu_2026.tif

- `mu` reads the `deterministic` variable; `var` reads `prediction_error_variance`.
  Mean and variance are always written to separate files.
- Works for multi-year hindcasts and for a single new forecast (a `T` of length 1,
  or no `T` at all; in that case pass `--years 2026`).
- Years are read from `T` unless `--years` is given.
- Latitude is sorted north-to-south; missing values (NaN or -999) become NaN nodata.
- CRS is assumed to be EPSG:4326 (plain lon/lat degrees). The Ethiopia OND test files
  are consistent with this, but the files carry no CRS metadata.
- Tests: `python -m pytest -v`
