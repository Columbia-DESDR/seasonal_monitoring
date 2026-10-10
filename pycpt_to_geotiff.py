"""
Convert PyCPT NetCDF output into a multiband GeoTIFF that Google Earth Engine
can ingest. Python port of forecast_formatting_multiband.R.

Handles both cases:
  - hindcasts: many years, dims (T, Y, X)  -> one band per year
  - new forecast: one year, dims (T, Y, X) with T of length 1, or (Y, X) -> one band

Usage:
  python pycpt_to_geotiff.py mu  in.nc out.tif
  python pycpt_to_geotiff.py var in.nc out.tif --years 2026   # if T has no usable dates
"""
import argparse

import numpy as np
import rasterio
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr

VARS = {
    "mu": "deterministic",
    "var": "prediction_error_variance",
}


def _years_from_T(da):
    """Season year of each T layer, or None if T is missing or not decodable."""
    if "T" not in da.dims:
        return None
    try:
        return [int(y) for y in da["T"].dt.year.values]
    except (AttributeError, TypeError):
        return None


def pycpt_to_geotiff(path, kind, out_path, years=None):
    """
    path     : PyCPT NetCDF file
    kind     : "mu" or "var"
    out_path : output .tif
    years    : optional labels, one per layer. If omitted, read from T.
    """
    if kind not in VARS:
        raise ValueError(f"kind must be one of {list(VARS)}")
    var_name = VARS[kind]

    with xr.open_dataset(path) as ds:
        if var_name not in ds.data_vars:
            raise KeyError(f"{var_name!r} not in file. Found: {list(ds.data_vars)}")
        da = ds[var_name].load()

    file_years = _years_from_T(da)
    if "T" not in da.dims:
        da = da.expand_dims("T")          # single forecast with no time axis

    n_layers = da.sizes["T"]
    if years is None:
        if file_years is None:
            raise ValueError("Could not read years from T; pass years explicitly.")
        years = file_years
    years = list(years)
    if len(years) != n_layers:
        raise ValueError(f"File has {n_layers} layers but {len(years)} year labels")

    da = da.where(da != -999)             # guard: PyCPT's documented missing value

    da = (
        da.reset_coords(drop=True)        # drop Ti, Tf, S etc.
          .rename({"X": "x", "Y": "y", "T": "band"})
          .sortby("y", ascending=False)   # north at the top
          .transpose("band", "y", "x")
          .assign_coords(band=np.arange(1, n_layers + 1))
          .rio.write_crs("EPSG:4326")     # assumption: plain lon/lat degrees
          .rio.write_nodata(np.nan)
    )
    da.rio.to_raster(out_path)

    with rasterio.open(out_path, "r+") as dst:
        for i, y in enumerate(years, start=1):
            dst.set_band_description(i, str(y))

    return out_path


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=list(VARS))
    p.add_argument("path")
    p.add_argument("out_path")
    p.add_argument("--years", nargs="+", type=int,
                   help="year labels, one per layer (default: read from T)")
    a = p.parse_args()
    print(pycpt_to_geotiff(a.path, a.kind, a.out_path, a.years))
