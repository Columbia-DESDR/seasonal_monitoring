import numpy as np
import pandas as pd
import pytest
import rasterio
import xarray as xr

from pycpt_to_geotiff import pycpt_to_geotiff

X = [33.125, 33.375, 33.625, 33.875]
Y_ASC = [3.125, 3.375, 3.625]  # south to north, so the converter must flip rows


def make_nc(path, var, values, dates=None, y=Y_ASC):
    if values.ndim == 3:
        ds = xr.Dataset({var: (("T", "Y", "X"), values)},
                        coords={"T": pd.to_datetime(dates), "Y": y, "X": X})
    else:
        ds = xr.Dataset({var: (("Y", "X"), values)}, coords={"Y": y, "X": X})
    ds.to_netcdf(path)


def test_multiyear_orientation_labels_and_georef(tmp_path):
    values = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    src, out = tmp_path / "in.nc", tmp_path / "out.tif"
    make_nc(src, "deterministic", values, ["1983-11-15", "1984-11-15"])

    pycpt_to_geotiff(str(src), "mu", str(out))

    with rasterio.open(out) as d:
        assert d.count == 2
        assert d.descriptions == ("1983", "1984")
        assert d.crs.to_epsg() == 4326
        assert (d.bounds.left, d.bounds.right) == pytest.approx((33.0, 34.0))
        assert (d.bounds.bottom, d.bounds.top) == pytest.approx((3.0, 3.75))
        # first TIFF row is the northernmost, so rows are reversed vs. the file
        np.testing.assert_array_equal(d.read(), values[:, ::-1, :])


def test_single_year_with_length_one_T(tmp_path):
    values = np.arange(12, dtype=np.float32).reshape(1, 3, 4)
    src, out = tmp_path / "in.nc", tmp_path / "out.tif"
    make_nc(src, "deterministic", values, ["2026-11-15"])

    pycpt_to_geotiff(str(src), "mu", str(out))

    with rasterio.open(out) as d:
        assert d.count == 1
        assert d.descriptions == ("2026",)


def test_no_T_dimension_needs_years_and_gives_one_band(tmp_path):
    values = np.arange(12, dtype=np.float32).reshape(3, 4)
    src, out = tmp_path / "in.nc", tmp_path / "out.tif"
    make_nc(src, "deterministic", values)

    with pytest.raises(ValueError):
        pycpt_to_geotiff(str(src), "mu", str(out))

    pycpt_to_geotiff(str(src), "mu", str(out), years=[2026])
    with rasterio.open(out) as d:
        assert d.count == 1
        assert d.descriptions == ("2026",)
        np.testing.assert_array_equal(d.read(1), values[::-1, :])


def test_variance_reads_the_variance_variable(tmp_path):
    values = np.arange(12, dtype=np.float32).reshape(1, 3, 4) + 100
    src, out = tmp_path / "in.nc", tmp_path / "out.tif"
    make_nc(src, "prediction_error_variance", values, ["2026-11-15"])

    pycpt_to_geotiff(str(src), "var", str(out))
    with rasterio.open(out) as d:
        np.testing.assert_array_equal(d.read(), values[:, ::-1, :])


def test_missing_values_become_nan(tmp_path):
    values = np.arange(12, dtype=np.float32).reshape(1, 3, 4)
    values[0, 0, 0] = -999
    values[0, 1, 1] = np.nan
    src, out = tmp_path / "in.nc", tmp_path / "out.tif"
    make_nc(src, "deterministic", values, ["2026-11-15"])

    pycpt_to_geotiff(str(src), "mu", str(out))
    with rasterio.open(out) as d:
        data = d.read(1)
        assert np.isnan(data).sum() == 2
        assert np.isnan(data[2, 0]) and np.isnan(data[1, 1])


def test_wrong_year_count_raises(tmp_path):
    values = np.zeros((2, 3, 4), dtype=np.float32)
    src = tmp_path / "in.nc"
    make_nc(src, "deterministic", values, ["1983-11-15", "1984-11-15"])
    with pytest.raises(ValueError):
        pycpt_to_geotiff(str(src), "mu", str(tmp_path / "o.tif"), years=[1983])


def test_wrong_variable_raises_with_helpful_message(tmp_path):
    values = np.zeros((1, 3, 4), dtype=np.float32)
    src = tmp_path / "in.nc"
    make_nc(src, "deterministic", values, ["2026-11-15"])
    with pytest.raises(KeyError, match="deterministic"):
        pycpt_to_geotiff(str(src), "var", str(tmp_path / "o.tif"))
