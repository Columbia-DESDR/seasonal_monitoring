# Yemen bulletin generator

Builds the seasonal monitoring bulletin as a PowerPoint deck, in the same
shape as the version previously assembled by hand: a title page, one slide
per variable with a 2x2 grid of maps, and a sources and method page.

Two steps, deliberately separate:

```
fetch.py       Earth Engine  ->  maps + manifest.json in output/
build_deck.py  manifest.json ->  .pptx (and .pdf with --pdf)
```

Keeping them apart means you can redo the slides as often as you like
without re-hitting Earth Engine, which is the slow part.

## Just want the bulletin?

Then skip all of this. It builds itself every month and you can download
the result, with nothing installed and nothing to sign into.

1. Actions tab in this repo
2. **Monthly bulletin** in the left sidebar
3. Open the most recent run, or press **Run workflow** for a fresh one,
   which takes about four minutes
4. On the run's Summary page, scroll to the bottom to **Artifacts**
5. Download, unzip, and the .pptx and .pdf are both in there

The rest of this README is for changing how the bulletin is built.

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

Two ways to reach Earth Engine. Pick one.

### With the service account (recommended)

Nothing to sign into. The key file is the credential, so there is no browser
step and no gcloud involved. This is also what the scheduled run uses, so
testing this way tests what actually runs.

Put three lines in `.env`:

```
EE_PROJECT=desdr-510913
EE_SERVICE_ACCOUNT_EMAIL=seasonal-monitoring@desdr-510913.iam.gserviceaccount.com
EE_SERVICE_ACCOUNT_KEY=/absolute/path/to/your-key.json
```

`EE_SERVICE_ACCOUNT_KEY` is a **path to the .json file**, not the email. Get
the key from the Keys tab of the service account in the Cloud console, and
keep it outside the repo. Both lines are needed: with only one set, the code
ignores the service account and signs in as you instead, which works on your
laptop and nowhere else.

### As yourself

```bash
python -c "import ee; ee.Authenticate()"
```

Only for this route, and only once. Leave the two service account lines
blank or it will be used instead.

This shells out to gcloud, so it inherits gcloud's problems. If you see
`ModuleNotFoundError: No module named 'imp'`, your gcloud is older than your
Python: `imp` was removed in Python 3.12 and older gcloud releases still
import it. Either point gcloud at an older interpreter, with
`export CLOUDSDK_PYTHON=/usr/bin/python3` in your shell profile, or update
the Cloud SDK. Or sidestep gcloud altogether with
`ee.Authenticate(auth_mode='notebook')`, which does the browser flow
directly. None of this applies on the service account route.

### Running it

```bash
rm -rf output                     # clear the placeholders
python fetch.py
python build_deck.py --pdf
```

The first line of output says which credential was used. Worth a glance:
`service account` or `personal login`.

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
not the end of the window shown. The hand-made bulletin was ambiguous about
which it meant, so this is worth confirming before anyone reads too much
into it.

## The scheduled run

`.github/workflows/bulletin.yml` runs the whole thing on the 10th of each
month, plus a button for running it on demand. The 10th because CHIRPS lags
about four weeks, so by then the previous month is reliably complete.
`fetch.py` works out the reporting month itself, so that date only has to be
late enough.

What a run does: installs the dependencies and LibreOffice, writes the
service account key to a temp file, then `fetch.py` and `build_deck.py --pdf`.
The deck and the PDF are uploaded as artifacts and kept for 90 days.

It signs in with the service account, so it is not tied to anyone's Google
account and keeps working whoever is around. The key lives in the repo's
**Settings, Secrets and variables, Actions** as `EE_SERVICE_ACCOUNT_KEY_JSON`,
holding the whole .json file as text. GitHub encrypts it and it cannot be
read back out, only replaced. The project id and the account email are in
the workflow file in plain text, which is fine: neither is a secret.

To change the schedule, edit the `cron` line. To stop it running
automatically while keeping the button, delete the `schedule` block.

A failed run emails whoever triggered it. Worth checking the Actions tab
around the 11th if nobody has seen a bulletin.

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

## Security

`.env`, any `*.json` credentials and the `output/` folder are gitignored.
Never commit a service account key. If one is ever pushed, rotate it rather
than deleting the file, because git keeps history.
