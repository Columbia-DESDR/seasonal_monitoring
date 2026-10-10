# Yemen bulletin generator

Builds the seasonal monitoring bulletin as a PowerPoint deck: a title page, one slide per variable with a 2x2 grid of maps, and a sources and method page. It follows the layout of the existing manually assembled bulletin.

The pipeline has two separate steps, so slides can be rebuilt without re-querying Earth Engine:

```
fetch.py       Earth Engine  ->  maps + manifest.json in output/
build_deck.py  manifest.json ->  .pptx (and .pdf with --pdf)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # set EE_PROJECT
```

To test the layout without Earth Engine:

```bash
python make_placeholders.py
python build_deck.py
```

## Running with Earth Engine

Authenticate once, using a Google Cloud project registered for Earth Engine with the API enabled. Set its id as `EE_PROJECT` in `.env`.

```bash
python -c "import ee; ee.Authenticate()"
rm -rf output                     # remove placeholders
python fetch.py
python build_deck.py --pdf
```

## Slide layout

Each variable has one slide with a 2x2 grid. Windows are trailing, so the 3-month panel covers the reported month and the two before it.

|  | 1-month | 3-month |
|---|---|---|
| **top row** | standardized anomaly | standardized anomaly |
| **bottom row** | % departure from normal | % departure from normal |

Vegetation uses VCI in the bottom row (`second_row` in the config).

## Configuration

`config.yaml` holds the country, month, variables (datasets, baseline years, palettes, bottom row) and boilerplate text. Targeting another country should only require editing this file.

## Reporting month

By default `fetch.py` queries Earth Engine for the data coverage of each dataset and reports on the most recent month fully covered by all of them. A month counts as covered only when data reaches its last day, so data through 29 September still gives an August bulletin. The dataset with the largest lag (usually CHIRPS, about four weeks) determines the month. The chosen month and the limiting dataset are printed on each run.

To rebuild an earlier bulletin, set both `bulletin.year` and `bulletin.month` in the config. The "data through" line on each slide shows the newest data in that dataset.

## Forecast slide

PyCPT forecast maps come from the maproom rather than Earth Engine. Place screenshots in `forecasts/` and list them under `forecasts.images` in the config. If the list is empty, the slide is skipped.

## Files

| File | Purpose |
|---|---|
| `config.yaml` | Region, month, variables, boilerplate text |
| `fetch.py` | Computes anomalies in Earth Engine and saves the maps |
| `build_deck.py` | Adds titles and colour bars, assembles the deck |
| `bands.py` | Titles and value ranges for each panel type |
| `make_placeholders.py` | Placeholder maps for offline layout work |
| `pycpt_to_geotiff.py` | Converts PyCPT NetCDF output to GeoTIFF |

## Relation to the GEE scripts

The `yemen_*_monitoring` scripts in `users/maxmauerman/wfp_yemen` each compute a standardized anomaly and export images to Drive. `fetch.py` performs the same calculation through the Python API and downloads the rendered maps directly over HTTP. The six per-variable, per-window scripts are replaced by one function parameterized by dataset, baseline years and window length. Panel titles and colour bars are drawn with matplotlib to match the existing deck.

## Security

`.env`, `*.json` credentials and `output/` are gitignored. Never commit a service account key. If one is pushed, rotate it rather than deleting the file, because git keeps history.

## Converting PyCPT forecasts to GeoTIFF

`pycpt_to_geotiff.py` converts PyCPT NetCDF output to a multiband GeoTIFF (one band per year, band names are the years) for Earth Engine ingestion.

    python pycpt_to_geotiff.py mu  MME_deterministic_hindcasts.nc output/hindcast_mu.tif
    python pycpt_to_geotiff.py var MME_hindcast_prediction_error_variance.nc output/hindcast_var.tif
    python pycpt_to_geotiff.py mu  MME_deterministic_forecast_2026.nc output/forecast_mu_2026.tif

- `mu` reads `deterministic`; `var` reads `prediction_error_variance`. Mean and variance are written to separate files.
- Supports multi-year hindcasts and single forecasts (`T` of length 1, or no `T`; pass `--years 2026` in the latter case).
- Years are read from `T` unless `--years` is given.
- Latitude is sorted north to south. Missing values (NaN or -999) become NaN nodata.
- The CRS is assumed to be EPSG:4326 (plain lon/lat). The Ethiopia OND test files are consistent with this but contain no CRS metadata.
- Tests: `python -m pytest -v`
