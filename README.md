# Seasonal monitoring bulletin

Builds a monthly seasonal monitoring bulletin as a PowerPoint deck:
title page, one slide per variable with a 2x2 grid of maps, a drought
forecast slide, a season-to-date slide, and a sources and method page.

One settings file per country in `configs/`, and one shared codebase.
Yemen and Ethiopia are set up. Every command takes `--config` to say
which country, and there is no default, so a run can never quietly
build the wrong one.

## Download the latest bulletin

If you only want the deck, you do not need any of this. It builds itself
monthly.

1. **Actions** tab, then **Monthly bulletin**
2. Open the most recent run, or press **Run workflow**
3. On the run's Summary page, scroll to **Artifacts**
4. Download and unzip. The .pptx and .pdf are both in there

## Requirements

- Python 3.11 or newer
- A Google account with Earth Engine enabled, and an Earth Engine
  project. Free for non-commercial use. All the data this reads is
  public.
- LibreOffice, only if you want PDFs

## Install

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Run `source venv/bin/activate` in every new terminal. Without it,
`python` is not the one with these packages.

To check the deck layout before setting up Earth Engine:

```bash
python make_placeholders.py --config configs/yemen.yaml   # fake maps
python build_deck.py --config configs/yemen.yaml
```

## Earth Engine access

### Your own account

This is the normal route. Use it unless you are setting up the
scheduled run.

```bash
python -c "import ee; ee.Authenticate(auth_mode='notebook')"
```

Once per machine, not per run. Then put your Earth Engine project in
`.env`:

```
EE_PROJECT=your-ee-project-id
```

Leave `EE_SERVICE_ACCOUNT_EMAIL` and `EE_SERVICE_ACCOUNT_KEY` blank.
Any organisation project works, and so does a personal one, because
every dataset here is public.

`auth_mode='notebook'` opens a browser directly. The default mode calls
gcloud instead and inherits its problems, which the troubleshooting
table below covers.

### Service account

For the scheduled run, and for anyone who already has access to the
cloud project. It needs no browser and is not tied to a person, so the
monthly build keeps working when people move on.

```
EE_PROJECT=your-project-id
EE_SERVICE_ACCOUNT_EMAIL=name@your-project-id.iam.gserviceaccount.com
EE_SERVICE_ACCOUNT_KEY=/absolute/path/to/key.json
```

`EE_SERVICE_ACCOUNT_KEY` is a path to the .json file, not the email.
Both account lines are needed: with only one set, the service account
is ignored and you are signed in as yourself instead.

**Do not share the key.** A service account key is a credential that
works for anyone holding it, with no second factor and no audit trail
back to a person. Testers and collaborators use their own Google
account. The key belongs in the repository secret and on the machine of
whoever administers the cloud project, nowhere else.

## Run

Four steps, in this order:

```bash
python fetch.py       --config configs/yemen.yaml
python observed.py    --config configs/yemen.yaml
python forecast.py    --config configs/yemen.yaml
python build_deck.py  --config configs/yemen.yaml
```

Swap `yemen` for `ethiopia` to build the other one. Each country reads
and writes its own folders, `forecasts/<country>/` and
`output/<country>/`, so they never overwrite each other.

| step | what it does | needs Earth Engine |
|---|---|---|
| `fetch.py` | pulls the rainfall, soil moisture and vegetation maps | yes |
| `observed.py` | pulls the rainfall record the drought threshold comes from, and the forecast if it is in Earth Engine | yes |
| `forecast.py` | computes the drought probability, the forecast maps and the two charts | no |
| `build_deck.py` | assembles everything into the .pptx | no |

Add `--pdf` to the last one for a PDF as well.

Without `forecast.py` there are no forecast slides. `build_deck.py` also
runs it for you, so the last two can be one command, but it is listed
here because it is a real step and the one people miss. Run it on its
own when you only want the forecast images and numbers without
rebuilding the deck.

`forecast.py` reads two things from disk: the PyCPT files named in
the config under `forecast_mu` and `forecast_var`, and the rainfall
record `observed.py` wrote. When the config sets `forecast_folder`,
`observed.py` downloads the newest forecast from Earth Engine to those
two paths, so nothing needs copying by hand.

### Where the forecast comes from

Forecasts are uploaded to Earth Engine under one naming rule:

```
<forecast_folder>/<COUNTRY>_pycpt_<SEASON>/issued_<YEAR>_<MONTH>
e.g. projects/desdr-510913/assets/forecasts/YEM_pycpt_JAS/issued_2026_05
```

- `COUNTRY` is the code in the config's `filter_value` (`YEM`, `ETH`)
- `SEASON` is the month initials (`JAS`, `OND`, `JJAS`)
- each image has two bands named `mu` and `var`
- set the start and end dates on upload when you can

`observed.py` takes the newest image that has both bands. The season
comes from the image's dates, or failing that from the names, and the
config's season settings are only a fallback. If no forecast is found
it says why, uses files already in `forecasts/<country>/` if there are
any, and otherwise the bulletin is built without the forecast slides. If either is missing it says which and
stops, and the deck is built without forecast slides.

The first line of output from `fetch.py` and `observed.py` names the
credential in use, `service account` or `personal login`.

`fetch.py` and `observed.py` are the slow steps. They are separate so
the deck can be rebuilt without going back to Earth Engine, and
`observed.py` only needs re-running when the season moves on.

### Regional charts

The two charts cover the whole country by default. Per governorate
instead:

```bash
python build_deck.py --config configs/yemen.yaml --list-regions
python build_deck.py --config configs/yemen.yaml --regions "Ta'izz Governorate" "Al Hudaydah Governorate"
```

Names or ids both work; quote names, which contain spaces and
apostrophes. Each region gets its own slide, laid out like the national
one. The maps stay national, since one governorate alone is too small
to read as a map, with the selected governorates outlined in black
and named.

Dropping the flag returns the national charts. Nothing is cached
between runs. `observed.py` pulls every governorate in one go, so it
only runs once whatever combination you later ask for.

## Output

Everything lands in `output/<country>/`.

| path | contents |
|---|---|
| `*_bulletin_YYYY_MM.pptx` | the deck |
| `*_bulletin_YYYY_MM.pdf` | with `--pdf` |
| `raw/` | map and chart images, before titling |
| `panels/` | titled panels as placed on slides |
| `manifest.json` | what `fetch.py` found and chose |
| `observed.json` | rainfall curves, national and per region |
| `forecast.json` | forecast numbers and panel list |

## Which month it reports on

Not configured. `fetch.py` asks Earth Engine what each dataset holds and
reports the most recent month all of them fully cover, since every slide
has to describe the same month. A month counts only when data reaches
its last day, so data through 29 September still means an August
bulletin.

Set `bulletin.year` and `bulletin.month` in the config to rebuild an
older one. Leave blank otherwise.

## What the slides hold

Each variable gets a 2x2 grid:

|  | 1-month | 3-month |
|---|---|---|
| **top** | standardized anomaly | standardized anomaly |
| **bottom** | % departure from normal | % departure from normal |

Vegetation swaps the bottom row for VCI, set by `second_row` in the
config. Windows are trailing, so the 3-month figure covers the reported
month and the two before it.

The forecast slide holds the drought probability map and a % departure
map. The season-to-date slide holds two charts:

**Forecast and rainfall so far.** The predicted season total, one
standard deviation either side, the drought threshold, and this
season's rain accumulating against it.

**Trigger probabilities.** Three distributions of the same quantity.
*Climatological* is the base rate and lands in one bin by construction;
if it does not, something upstream is wrong. *Forecast* is the map on
the previous slide. *Conditional* finds past seasons that had about as
much rain by this point and counts how many finished below the
threshold, with no model in it. The conditional series is the only one
that settles as the season runs.

## How the forecast is computed

The question is the chance the season comes in dry enough to count as a
drought.

1. **Threshold**: the 20th percentile of observed seasonal rainfall per
   pixel, so drought means dry for that place rather than below a fixed
   number of millimetres.
2. **Z-score**: `(threshold - forecast mean) / sqrt(prediction error
   variance)`.
3. **Normal CDF** of that z-score.

The variance is not optional. A single predicted number can only be
above or below the threshold; a distribution can be asked how much of
itself falls below it. Because the threshold is the 20th percentile,
0.20 is the no-information baseline and higher means elevated risk.

**The threshold must come from observations, not the hindcast.** The
prediction error variance measures the model against observed rainfall,
so the threshold has to come from the same distribution. A hindcast
holds the model's own retrospective predictions, which regress toward
the mean, so its spread is too narrow and every probability comes out
too high.

**Resolution.** The forecast is a 1 degree grid, rainfall is 0.05
degrees. Averaging rainfall down to 1 degree would smooth away most of
the variation the threshold exists to capture and leave the country
with a few dozen cells. The coarse forecast is carried up onto the
rainfall grid instead. `observed.py` works that grid out from the
country outline, so there is nothing to type. One
consequence is that the calibration check then compares a 1 degree
quantity against a 0.05 degree one, which is not like for like; the run
reports the number and says so rather than treating it as a fault.

**Calibration check.** Every run places a forecast carrying no
information at the climatological mean and asks for the probability,
which should come back as the base rate. When the grids match and the
gap exceeds 0.05, the run warns and marks the slide provisional.

**Input files.** The forecast is PyCPT output, two GeoTIFFs in
`forecasts/` named in the config. Clear those paths to skip the forecast
slides. PyCPT coordinates are pixel centres, and a converter that treats
them as the raster's outer edge shifts the whole grid half a cell, about
50 km for Yemen. `forecast.py` detects that and corrects it on read,
says so, and leaves the file on disk alone.

## Configuration

Everything is in the country's file in `configs/`. The keys worth knowing:

| key | what it does |
|---|---|
| `region` | country name and boundary collection |
| `layers` | the variables, their datasets and baseline years |
| `analysis_grid` | optional. The grid everything is computed on, worked out from the country outline when left unset |
| `season_start_day`, `season_length_days` | which days of the year the season covers |
| `forecast_folder` | optional. Earth Engine folder holding the `<COUNTRY>_pycpt_<SEASON>` collections. Leave blank to use local files only |
| `forecast_mu`, `forecast_var` | the PyCPT files, `.tif` or `.tiff`. Where the download lands when `forecast_folder` is set |
| `forecasts.dir`, `output.dir` | this country's own folders |
| `mean_scale`, `variance_scale` | unit corrections on those files |
| `threshold_percentile` | what counts as a drought, 20 by default |
| `analog_window_mm` | how close a past year must be to count as an analog |
| `bounds_z` | width of the chart's band, in standard deviations |
| `min_threshold_mm` | blank pixels whose threshold is under this |
| `regional_data` | pull per-governorate data, for `--regions` |

On `min_threshold_mm`: where a place gets almost no rain, the percentile
lands on zero, nothing can fall below zero, and the driest cells in the
country read as carrying no drought risk. A cutoff blanks them instead.
It is 0 by default to match the reference dashboard; raise it if those
cells mislead readers.

## Adding a country

1. Copy an existing file: `cp configs/yemen.yaml configs/kenya.yaml`
2. In the copy, change `region.name`, `region.filter_value` (the
   three-letter country code), `bulletin.title`, the season, the
   forecast file paths, and `forecasts.dir` and `output.dir` so they
   end in the new country's name
3. Check `mean_scale` and `variance_scale`. PyCPT output is a season
   total for some domains and a monthly average for others, so these
   are per country
4. Put the two PyCPT files in `forecasts/<country>/`
5. Add the name to the `country:` list in the workflow

There are no grid numbers to work out. `observed.py` takes the country's
bounding box from the boundary data and snaps it to whole rainfall
cells, and prints what it chose.

A region inside a country is not a new config. It is the `--regions`
flag on that country's config.

## Scheduled run

`.github/workflows/bulletin.yml` runs monthly on the 10th, plus a button
for running on demand. The 10th because rainfall data lags about four
weeks.

It signs in with the service account, so it does not depend on anyone's
personal account. The key goes in the repository settings, under
Secrets, as `EE_SERVICE_ACCOUNT_KEY_JSON`: the whole key file as text.
GitHub encrypts it and it cannot be read back, only replaced.

The project id and the service account address are typed at the top of
the workflow file. They are not secret; without the key they let
nobody in.

It runs once per country listed under `matrix.country` in the workflow,
side by side, and uploads one artifact each, `bulletin-yemen` and so
on. One country failing does not stop the others.

The run installs dependencies and LibreOffice, writes the key to a temp
file, then runs the pipeline and uploads the deck and PDF as artifacts,
kept 90 days. When a country has no forecast, `observed.py`
says so and the bulletin still builds without the forecast slides. The service account needs read access to
the forecast asset.

Edit the `cron` line to change the schedule. Delete the `schedule` block
to keep the button but stop it running on its own. A failed run emails
whoever triggered it.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `pyenv: python: command not found` | the venv is not active. `source venv/bin/activate` |
| `ModuleNotFoundError: No module named 'ee'` | same |
| `ModuleNotFoundError: No module named 'imp'` | gcloud is older than Python 3.12. Use `auth_mode='notebook'`, or `export CLOUDSDK_PYTHON=/usr/bin/python3`, or update the Cloud SDK |
| `FileNotFoundError` naming an email address | `EE_SERVICE_ACCOUNT_KEY` and `EE_SERVICE_ACCOUNT_EMAIL` are swapped in `.env` |
| Forecast slides missing, no error | `forecast.py` did not run, or the files named in the config are not on disk. Its output says which. `.tif` and `.tiff` are both accepted |
| `carries no usable data: every cell holds the same value` | the GeoTIFF conversion failed and wrote an integer raster, so NaN became that type's maximum. Reconvert from the NetCDF |
| `Too many concurrent aggregations` | Earth Engine is busy. The code waits and retries; if it still fails, run again later |
| `historical_curves keyed by ... rather than by year` | stale `observed.json`. Re-run `observed.py` |
| LibreOffice killed on macOS | Gatekeeper quarantine. `xattr -dr com.apple.quarantine /Applications/LibreOffice.app` |
| Deck identical after changing inputs | a previous `forecast.json` was reused. The run says when it clears one |

## Files

| file | what it does |
|---|---|
| `configs/<country>.yaml` | all settings for one country. The only files you normally edit |
| `settings.py` | reads a config; shared by every script |
| `fetch.py` | pulls the rainfall, soil moisture and vegetation maps |
| `observed.py` | pulls the rainfall record and per-region curves |
| `forecast.py` | computes the drought probability and the charts |
| `build_deck.py` | titles the panels and assembles the deck |
| `charts.py` | draws the two charts |
| `bands.py` | titles and value ranges per panel type |
| `make_placeholders.py` | fake data for working on layout offline |

## Security

`.env`, any `*.json` credentials and `output/` are gitignored. The
blanket `*.json` rule is deliberate: a service account key is named
after the project and a random hash, so no narrower pattern catches it
reliably.

Never commit a service account key, and never send one over chat or
email. If one is pushed, rotate it rather than deleting the file, since
git keeps history.