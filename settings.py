"""
Loading a country's config.

Its own module rather than living in fetch.py, because forecast.py and
build_deck.py need it and deliberately never import Earth Engine. That
separation is worth keeping: it means a deck can be rebuilt from rasters
already on disk without credentials, or even without the Earth Engine
library installed.

One config file per country, one shared codebase. Adding a country means
adding a file, not a branch or a fork, so a bug fixed once is fixed
everywhere. That is the whole reason these scripts take a path at all.
"""

import argparse
from pathlib import Path

import yaml


BASE_DIR = Path(__file__).parent


def load_config(path):
    """Read one country's settings.

    Relative paths resolve against the repo rather than the working
    directory, so the scripts behave the same wherever they are run
    from, including from a scheduled job.
    """
    path = Path(path)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        available = sorted(f"configs/{p.name}"
                           for p in (BASE_DIR / "configs").glob("*.yaml"))
        raise SystemExit(
            f"No config at {path}."
            + (f"\n\nThere is: {', '.join(available)}" if available
               else "\n\nNo .yaml files found in the project either.")
        )

    with open(path) as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise SystemExit(f"{path} did not parse as a set of settings. "
                         f"Check its indentation.")
    return config


def config_argument(parser):
    """Add --config, worded the same way in every script."""
    parser.add_argument(
        "--config", required=True, metavar="FILE",
        help="which country's settings to use, for example "
             "configs/yemen.yaml. Required, with no default, so a run "
             "can never quietly build the wrong country.")
    return parser
