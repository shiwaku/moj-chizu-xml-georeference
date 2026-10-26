"""2段構えの位置合わせ: 地番ジオコードで粗く合わせ、DEM・筆界で細かく合わせる。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely import unary_union
from shapely.affinity import affine_transform
from shapely.geometry.base import BaseGeometry

from moj_georef import anchor, reference, search
from moj_georef.geocode import GsiGeocoder
from moj_georef.parcels import MapXml, Parcel
from moj_georef.reference import PublicParcels


@dataclass
class Area:
    id: str
    xml: MapXml
    parcels: list[Parcel]
    keys: list[str] = field(default_factory=list)  # ジオコードできた「大字+本番」
    src: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))  # 局所座標の重心
    dst: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))  # ジオコード結果
    coarse: anchor.AnchorFit | None = None
    n_unique: int = 0  # 重複をまとめた後の対応点数
    final: search.Candidate | None = None
    second_gap: float = float("nan")

    @property
    def status(self) -> str:
        if self.final is not None:
            return "refined"
        if self.coarse is not None and self.coarse.transform is not None:
            return "coarse_only"
        return "skipped"

    @property
    def transform(self) -> search.Similarity | None:
        if self.final is not None:
            return self.final.transform
        return self.coarse.transform if self.coarse else None

    def geoms(self, t: search.Similarity | None = None) -> list[BaseGeometry]:
        t = t or self.transform
        assert t is not None
        params = t.affine_params()
        return [affine_transform(p.geometry, params) for p in self.parcels]


def build_areas(
    xmls: list[MapXml],
    geocoder: GsiGeocoder,
    pref_name: str,
    epsg: int,
    gap: float = 15.0,
) -> list[Area]:
    to_xy = Transformer.from_crs("EPSG:6668", f"EPSG:{epsg}", always_xy=True)
    areas: list[Area] = []
    for m in xmls:
        pref_city = pref_name + m.header["市区町村名"]
        for i, ps in enumerate(anchor.split_areas(m.parcels, gap)):
            a = Area(f"{m.file}#{i}", m, ps)
            keys, src, dst = [], [], []
            for (oaza, num), c in anchor.honban_centroids(ps).items():
                r = geocoder.banchi(pref_city, oaza, num)
                if r is not None:
                    keys.append(f"{oaza}{num}")
                    src.append(c.coords[0])
                    dst.append(to_xy.transform(r.lon, r.lat))
            a.keys, a.src, a.dst = keys, np.array(src).reshape(-1, 2), np.array(dst).reshape(-1, 2)
            us, ud = anchor.merge_duplicates(a.src, a.dst) if len(src) else (a.src, a.dst)
            a.n_unique = len(us)
            # 縮尺は 1 に固定し、位置と回転だけを取る（丸められたジオコードで縮尺が縮むため）
            a.coarse = anchor.robust_fit(us, ud, fix_scale=True)
            areas.append(a)
    return areas


def refine(
    area: Area,
    public: PublicParcels,
    dem_path: str | Path,
    placed: list[BaseGeometry],
    shift: float = 80.0,
    dtheta: float = 8.0,
    scales: np.ndarray | None = None,
) -> None:
    """粗い合わせの結果の周り（±shift [m]・±dtheta [deg]）だけを DEM・筆界で探索する。"""
    init = area.coarse.transform if area.coarse else None
    if init is None:
        return
    scales = np.array([0.98, 0.99, 1.0, 1.01, 1.02]) if scales is None else scales
    moved = unary_union(area.geoms(init))
    x0, y0, x1, y1 = moved.bounds
    m = shift + 20
    bounds = (x0 - m, y0 - m, x1 + m, y1 + m)
    ref = reference.build(dem_path, bounds, public, placed=placed)

    local = [p.geometry for p in area.parcels]
    bd = search.sample_boundary(local)
    it = search.sample_interior(local)
    thetas = init.theta_deg + np.arange(-dtheta, dtheta + 0.5, 1.0)
    cands = search.global_search(
        bd, ref.field, thetas, scales, interior=it, penalty=ref.penalty, top_k=5
    )
    # 初期位置から shift を超えて離れた候補は捨てる
    c0 = init.apply(bd.xy.mean(axis=0, keepdims=True))[0]
    cands = [
        c
        for c in cands
        if np.hypot(*(c.transform.apply(bd.xy.mean(axis=0, keepdims=True))[0] - c0)) <= shift
    ]
    if not cands:
        return
    refined = sorted(
        (
            search.refine(
                bd, ref.field, c.transform, it, ref.penalty,
                theta_bounds=(thetas.min() - 1, thetas.max() + 1),
                scale_bounds=(scales.min(), scales.max()),
            )
            for c in cands
        ),
        key=lambda c: c.score + c.overlap,
    )  # fmt: skip
    area.final = refined[0]
    if len(refined) > 1:
        area.second_gap = (refined[1].score + refined[1].overlap) - (
            refined[0].score + refined[0].overlap
        )
