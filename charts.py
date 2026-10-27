"""
The two charts on the forecast slide.

These reproduce the charts from the Code Editor dashboard: same series,
same names, same colours, same axes. They are the lab's charts and people
already read them a particular way, so they are copied rather than
redesigned.

Colours are the Google Charts defaults the originals were drawn with, so
a reader moving between the dashboard and the deck sees the same thing.
"""

from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter


# Google Charts' classic series colours, which is what the originals use.
LOWER = "#3366cc"
UPPER = "#dc3912"
FCAST = "#ff9900"
CURRENT = "#109618"
TRIGGER = "#990099"

# The probability chart uses the Google brand four instead.
CLIM = "#4285f4"
CONDTL = "#db4437"
FCAST_PROB = "#f4b400"

# Kept so forecast.py can go on importing these names.
FORECAST = FCAST
OBSERVED = CURRENT
THRESHOLD = TRIGGER
HISTORY = CLIM

INK = "#222222"
MUTED = "#666666"
GRID = "#e8e8e8"

TITLE_SIZE = 7.5
LABEL_SIZE = 5.0
TICK_SIZE = 4.5


def _frame(width, height, dpi, title, rect):
    """A panel titled and gridded the way the originals are: title at the
    top left rather than centred, and the legend in a column on the right."""
    fig = plt.figure(figsize=(width, height), dpi=dpi)
    fig.patch.set_facecolor("white")
    fig.text(rect[0], 0.94, title, ha="left", va="center",
             fontsize=TITLE_SIZE, fontweight="bold", color=INK)

    ax = fig.add_axes(rect)
    ax.set_facecolor("white")
    ax.grid(True, color=GRID, linewidth=0.4)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#cccccc")
        ax.spines[side].set_linewidth(0.5)
    ax.tick_params(labelsize=TICK_SIZE, length=2, width=0.4, colors=MUTED)
    return fig, ax


def _right_legend(ax):
    """Legend in a column outside the plot on the right, as in the
    originals."""
    legend = ax.legend(fontsize=LABEL_SIZE, frameon=False,
                       loc="upper left", bbox_to_anchor=(1.02, 1.0),
                       handlelength=1.8, handletextpad=0.5,
                       labelspacing=0.5, borderpad=0)
    for text in legend.get_texts():
        text.set_color(INK)
    return legend


def season_to_date(out_path, days, observed_mm, forecast_mm, lower_mm,
                   upper_mm, threshold_mm, title, width, height, dpi=300,
                   lag_days=None, season_start=None, season_length=None,
                   historical=None):
    """Forecast and rainfall so far this year.

    Five series, as in the original: the forecast with a bound either
    side, the trigger threshold, and this season's rainfall accumulating.
    The four flat ones are season totals, so they describe where the
    season ends rather than how it gets there. The x axis is day of year,
    which is what the original uses.

    historical is accepted and ignored. It is pulled by observed.py and
    available in observed.json if it is ever wanted here.
    """
    fig, ax = _frame(width, height, dpi, title, [0.13, 0.17, 0.62, 0.70])

    xs = [date.fromisoformat(d).timetuple().tm_yday for d in days]

    ax.axhline(lower_mm, color=LOWER, linewidth=1.0,
               label="Fcast lower bound")
    ax.axhline(upper_mm, color=UPPER, linewidth=1.0,
               label="Fcast upper bound")
    ax.axhline(forecast_mm, color=FCAST, linewidth=1.0, label="Fcast")
    if xs:
        ax.plot(xs, observed_mm, color=CURRENT, linewidth=1.0,
                label="Current season")
    ax.axhline(threshold_mm, color=TRIGGER, linewidth=1.0,
               label="Trig. threshold")

    ax.set_ylabel("Cumulative rainfall (mm)", fontsize=LABEL_SIZE,
                  color=INK, style="italic")
    ax.set_ylim(bottom=0)

    # The x axis covers the season, so a line part way through reads as
    # part way through.
    if season_start and season_length:
        first = date.fromisoformat(season_start).timetuple().tm_yday
        ax.set_xlim(first, first + season_length - 1)
    elif xs:
        ax.set_xlim(min(xs), max(xs))

    _right_legend(ax)

    if not xs:
        ax.text(0.5, 0.5, "No rainfall recorded in this season yet",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=LABEL_SIZE + 0.5, color=MUTED)

    fig.savefig(out_path, dpi=dpi, facecolor="white")
    plt.close(fig)


def probability_distributions(out_path, series, title, width, height,
                              dpi=300, bins=10, weight=1.0):
    """Trigger probabilities, as a frequency histogram.

    Three series over the same bins, filled and overlaid, as in the
    original. The y axis is a count of pixels.

    weight is how many of the original's pixels each of ours stands for.
    The dashboard counts at 1 km while this works on the rainfall grid,
    about 5.4 km, so one cell here is roughly thirty there. Counting the
    cells we have would put the same distribution on an axis thirty times
    shorter, which reads as a different answer when the two charts are
    held side by side. Weighting by cell area puts them on one scale
    without touching the shape.
    """
    fig, ax = _frame(width, height, dpi, title, [0.15, 0.22, 0.60, 0.65])
    edges = np.linspace(0, 1, bins + 1)

    for label, values, colour in series:
        clean = np.asarray(values)
        clean = clean[np.isfinite(clean)]
        if clean.size == 0:
            continue
        weights = (np.full(clean.shape, float(weight))
                   if weight != 1.0 else None)
        ax.hist(clean, bins=edges, weights=weights, histtype="stepfilled",
                color=colour, alpha=0.45, linewidth=0)
        ax.hist(clean, bins=edges, weights=weights, histtype="step",
                color=colour, linewidth=1.0, label=label)

    ax.set_ylabel("Frequency", fontsize=LABEL_SIZE, color=INK,
                  style="italic")
    ax.set_xlim(0, 1)
    ax.set_xticks(edges)
    ax.set_xticklabels([f"{lo:.3f} -\n{hi:.3f}"
                        for lo, hi in zip(edges[:-1], edges[1:])] + [""],
                       fontsize=TICK_SIZE - 0.7)
    # Shift the bin labels into the middle of their bins, since they
    # describe a range rather than an edge.
    for tick in ax.get_xticklabels():
        tick.set_horizontalalignment("left")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))

    _right_legend(ax)
    fig.savefig(out_path, dpi=dpi, facecolor="white")
    plt.close(fig)