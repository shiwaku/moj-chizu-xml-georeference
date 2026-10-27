"""DEM から位置合わせ用の参照画像（エッジ・距離画像）を作る。

県全体の DEM（タイル分割された GeoTIFF / VRT）から、探索範囲だけを窓読みする。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.windows import from_bounds
from scipy import ndimage

Bounds = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)

DEFAULT_DEM = os.environ.get("MOJ_GEOREF_DEM", "")

_ROWS = "ABCDEFGHIJKLMNOPQRST"  # 北から 30km ごと
_COLS = "ABCDEFGH"  # 西から 40km ごと


def sheet_2500(x: float, y: float) -> str:
    """平面直角座標 (x=東, y=北) を含む国土基本図図郭（地図情報レベル2500）の番号。

    例: (36945, -185435) → "QE193"（座標系番号は付けない）
    """
    r50 = int((300_000 - y) // 30_000)
    c50 = int((x + 160_000) // 40_000)
    top, left = 300_000 - r50 * 30_000, -160_000 + c50 * 40_000
    r5, c5 = int((top - y) // 3_000), int((x - left) // 4_000)
    top5, left5 = top - r5 * 3_000, left + c5 * 4_000
    south, east = (top5 - y) >= 1_500, (x - left5) >= 2_000
    return f"{_ROWS[r50]}{_COLS[c50]}{r5}{c5}{1 + int(east) + 2 * int(south)}"


def sheets_2500(bounds: Bounds) -> list[str]:
    """範囲にかかる図郭（レベル2500: 東西 2km × 南北 1.5km）の一覧。"""
    xmin, ymin, xmax, ymax = bounds
    xs = np.arange(xmin - xmin % 2_000, xmax, 2_000)
    ys = np.arange(ymin - ymin % 1_500, ymax, 1_500)
    names = {sheet_2500(x + 1, y + 1) for x in xs for y in ys}
    return sorted(names)


@dataclass
class Grid:
    """北が上の等間隔グリッド。array[row, col]。"""

    array: np.ndarray
    transform: Affine

    @property
    def res(self) -> float:
        return self.transform.a

    @property
    def bounds(self) -> Bounds:
        # affine 2.x（QGIS 同梱）は @ を持たず、3.x は * を非推奨にしているので係数で計算する
        h, w = self.array.shape
        t = self.transform
        x0, y0 = t.c, t.f
        x1, y1 = t.a * w + t.b * h + t.c, t.d * w + t.e * h + t.f
        return (x0, y1, x1, y0)

    def write(self, path: Path, crs: str = "EPSG:6671") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        arr = self.array.astype("float32") if self.array.dtype == bool else self.array
        h, w = arr.shape
        with rasterio.open(
            path, "w", driver="GTiff", width=w, height=h, count=1, dtype=arr.dtype,
            crs=crs, transform=self.transform, compress="deflate", tiled=True,
        ) as dst:  # fmt: skip
            dst.write(arr, 1)


def read_dem(dem_path: str | Path, bounds: Bounds) -> Grid:
    with rasterio.open(dem_path) as src:
        win = from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        arr = src.read(1, window=win, masked=True).astype("float32")
        transform = src.window_transform(win)
    return Grid(arr.filled(np.nan), transform)


def slope_deg(dem: Grid, sigma_px: float = 1.0) -> Grid:
    z = dem.array
    filled = np.where(np.isnan(z), np.nanmean(z), z)
    if sigma_px > 0:
        filled = ndimage.gaussian_filter(filled, sigma_px)
    gy, gx = np.gradient(filled, dem.res)
    s = np.degrees(np.arctan(np.hypot(gx, gy)))
    s[np.isnan(z)] = np.nan
    return Grid(s.astype("float32"), dem.transform)


def curvature(dem: Grid, sigma_m: float = 1.0) -> Grid:
    """ガウシアンで平滑化したラプラシアン（凸で負・凹で正）[1/m]。"""
    z = dem.array
    filled = np.where(np.isnan(z), np.nanmean(z), z)
    c = ndimage.gaussian_laplace(filled, sigma_m / dem.res) / dem.res**2
    c[np.isnan(z)] = np.nan
    return Grid(c.astype("float32"), dem.transform)


def terrain_edges(
    dem: Grid, sigma_m: float = 1.0, percentile: float = 90.0, min_px: int = 8
) -> Grid:
    """傾斜の変わり目（法肩・法尻・尾根・谷・段差）をエッジとして返す。

    曲率の絶対値が上位 (100 - percentile)% の画素をエッジにし、小さな塊は除く。
    造成地や棚田では、筆界が法肩・法尻に来ることが多い。
    """
    c = np.abs(curvature(dem, sigma_m).array)
    thr = np.nanpercentile(c, percentile)
    edges = np.nan_to_num(c, nan=0.0) >= thr
    labels, n = ndimage.label(edges)
    if n:
        sizes = ndimage.sum(edges, labels, index=np.arange(1, n + 1))
        edges = np.isin(labels, np.flatnonzero(sizes >= min_px) + 1)
    return Grid(edges, dem.transform)


def distance_to(edges: Grid, max_dist: float) -> Grid:
    """最寄りのエッジまでの距離 [m]（max_dist で打ち切り）。"""
    d = ndimage.distance_transform_edt(~edges.array.astype(bool)) * edges.res
    return Grid(np.minimum(d, max_dist).astype("float32"), edges.transform)


def line_orientation(values: Grid, sigma_m: float = 1.5) -> Grid:
    """画像中の線状の構造の向き [deg]（0〜180、東=0、反時計回り）を構造テンソルで求める。

    曲率画像に使うと、法肩・段差（等高線方向）も尾根・谷（斜面方向）も、線そのものの向きが出る。
    """
    v = np.nan_to_num(values.array, nan=0.0)
    gr, gc = np.gradient(v)  # 行方向（南向き）・列方向（東向き）
    gx, gy = gc, -gr
    s = sigma_m / values.res
    jxx = ndimage.gaussian_filter(gx * gx, s)
    jyy = ndimage.gaussian_filter(gy * gy, s)
    jxy = ndimage.gaussian_filter(gx * gy, s)
    grad_dir = 0.5 * np.degrees(np.arctan2(2 * jxy, jxx - jyy))  # 勾配の向き
    return Grid(np.mod(grad_dir + 90.0, 180.0).astype("float32"), values.transform)


def oriented_distances(
    edges: Grid, angle: Grid, n_bins: int, tolerance: float, max_dist: float
) -> list[Grid]:
    """向きの区分ごとに「向きの差が tolerance 以内のエッジ」までの距離画像を作る。"""
    out = []
    for k in range(n_bins):
        center = k * 180.0 / n_bins
        diff = np.abs((angle.array - center + 90.0) % 180.0 - 90.0)
        mask = edges.array.astype(bool) & (diff <= tolerance)
        out.append(distance_to(Grid(mask, edges.transform), max_dist))
    return out
