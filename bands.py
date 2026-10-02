"""
What each panel shows, and how it is labelled on the slide.

Kept free of Earth Engine imports so the deck side never needs it.
"""

BAND_SPECS = {
    "standardized": {
        "min": -3.0,
        "max": 3.0,
        # {label} is the variable name, e.g. Rainfall
        "title": "Standardized {label}, {window}",
        "units": "standard deviations from normal",
    },
    "anomaly_pct": {
        "min": -100,
        "max": 100,
        "title": "{label}, % departure from normal, {window}",
        "units": "percent departure from normal",
    },
    "vci": {
        "min": 0,
        "max": 100,
        "title": "Vegetation Condition Index, {window}",
        "units": "0 = worst on record, 100 = best",
    },
}


def rows_for(layer):
    """The two bands that make up a variable's slide, top row then bottom."""
    return ["standardized", layer.get("second_row", "anomaly_pct")]


def panel_title(band, layer_label, window_label):
    return BAND_SPECS[band]["title"].format(label=layer_label,
                                            window=window_label)
