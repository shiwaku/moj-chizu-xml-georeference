"""位置合わせの参照画像（距離画像・罰則画像）を作る。

- エッジ = DEM の曲率エッジ ∪ 公共座標系の筆界
- 罰則 = 公共座標系の筆の内部（重なってはいけない）＋ エッジの密度（筆の内部は平らなはず）

公共座標系のデータには、地籍調査の区域外を図郭単位の長方形で埋めた擬似的な筆
（地番が「地区外-…」）が含まれる。その線は実在しないので、エッジにも禁止領域にも使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np
from rasterio.features import rasterize
from scipy import ndimage
from shapely import STRtree
from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry

from moj_georef import dem
from moj_georef.dem import Bounds, Grid
from moj_georef.search import DistanceField

PSEUDO_PREFIXES = ("地区外",)


def is_pseudo(chiban: str) -> bool:
    return chiban.startswith(PSEUDO_PREFIXES)


@dataclass
class Reference:
    field: DistanceField
    penalty: Grid  # 筆の内部点で平均を取り、評価関数に足す
    edges: Grid
    angle: Grid


class PublicParcels:
    """公共座標系の筆（擬似筆を除く）を空間索引付きで保持する。"""

    def __init__(self, geoms: list[BaseGeometry], keys: list[str]):
        self.geoms = geoms
        self.keys = keys  # 除外指定に使うキー（例: XML ファイル名）
        self.tree = STRtree(geoms)

    def query(self, bounds: Bounds, exclude: set[str] = frozenset()) -> list[BaseGeometry]:
        idx = self.tree.query(box(*bounds))
        return [self.geoms[i] for i in idx if self.keys[i] not in exclude]


def build(
    dem_path: str | Path,
    bounds: Bounds,
    public: PublicParcels,
    exclude: set[str] = frozenset(),
    max_dist: float = 10.0,
    edge_percentile: float = 90.0,
    edge_min_px: int = 40,
    forbidden_inset: float = 1.5,
    forbidden_weight: float = 10.0,
    clutter_weight: float = 3.0,
    n_bins: int = 6,
    angle_tolerance: float = 30.0,
    placed: list[BaseGeometry] | None = None,
) -> Reference:
    """placed: すでに位置合わせ済みのエリア（重なってはいけないので禁止領域に加える）。"""
    g = dem.read_dem(dem_path, bounds)
    shape, tf = g.array.shape, g.transform
    curv = dem.curvature(g, sigma_m=1.0)
    terrain = dem.terrain_edges(g, sigma_m=1.0, percentile=edge_percentile, min_px=edge_min_px)
    angle = dem.line_orientation(Grid(np.abs(curv.array), tf)).array
    edges_arr = terrain.array.astype(bool)

    # 公共座標系の筆界は線分ごとの向きを焼き込み、地形エッジより優先する
    pubs = public.query(bounds, exclude)
    segs = [
        (LineString([a, b]).buffer(0.3), np.degrees(np.arctan2(b[1] - a[1], b[0] - a[0])) % 180)
        for p in pubs
        for a, b in pairwise(p.exterior.coords)
        if a != b
    ]
    if segs:
        pub_angle = rasterize(segs, out_shape=shape, transform=tf, fill=-1.0, dtype="float32")
        on_line = pub_angle >= 0
        edges_arr |= on_line
        angle = np.where(on_line, pub_angle, angle)
    edges = Grid(edges_arr, tf)
    angle_grid = Grid(angle.astype("float32"), tf)
    field = DistanceField(
        dem.oriented_distances(edges, angle_grid, n_bins, angle_tolerance, max_dist), max_dist
    )
    others = pubs + list(placed or [])
    insets = [x for p in others if not (x := p.buffer(-forbidden_inset)).is_empty]
    forb = rasterize(insets, out_shape=shape, transform=tf) if insets else np.zeros(shape)
    # 2m 四方のエッジ密度（森林の雑音や、筆の内部を横切る段差を検出する）
    k = max(int(round(2.0 / g.res)), 1)
    clutter = ndimage.uniform_filter(edges.array.astype("float32"), size=k)
    penalty = forbidden_weight * forb + clutter_weight * clutter
    return Reference(field, Grid(penalty.astype("float32"), tf), edges, angle_grid)
