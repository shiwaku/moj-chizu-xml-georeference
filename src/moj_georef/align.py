"""任意座標系の地区を位置合わせする。

探索範囲は、同じ市区町村の公共座標系の筆のうち、同じ大字を持つものの範囲を広げて決める。
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry.base import BaseGeometry

from moj_georef import parcels, reference, search
from moj_georef.dem import Bounds
from moj_georef.reference import PublicParcels, is_pseudo


def normalize_oaza(name: str) -> str:
    return name.strip().removeprefix("大字")


@dataclass
class City:
    public: PublicParcels
    oaza_bounds: dict[str, Bounds]
    arbitrary: list[parcels.MapXml]


def load_city(zip_path: Path) -> City:
    geoms: list[BaseGeometry] = []
    keys: list[str] = []
    by_oaza: dict[str, list[tuple[float, float, float, float]]] = {}
    arbitrary: list[parcels.MapXml] = []
    for m in parcels.iter_city_zip(zip_path):
        if not m.epsg:
            arbitrary.append(m)
            continue
        for p in m.parcels:
            if is_pseudo(p.attrs.get("地番", "")):
                continue
            geoms.append(p.geometry)
            keys.append(m.file)
            oaza = normalize_oaza(p.attrs.get("大字名", ""))
            if oaza:
                by_oaza.setdefault(oaza, []).append(p.geometry.bounds)
    oaza_bounds = {}
    for oaza, bs in by_oaza.items():
        a = np.array(bs)
        oaza_bounds[oaza] = (a[:, 0].min(), a[:, 1].min(), a[:, 2].max(), a[:, 3].max())
    return City(PublicParcels(geoms, keys), oaza_bounds, arbitrary)


def search_bounds(m: parcels.MapXml, city: City, margin: float) -> Bounds | None:
    """XML に出てくる大字の公共座標系の範囲を合わせ、margin [m] 広げる。"""
    names = Counter(normalize_oaza(p.attrs.get("大字名", "")) for p in m.parcels)
    found = [city.oaza_bounds[n] for n in names if n in city.oaza_bounds]
    if not found:
        return None
    a = np.array(found)
    return (
        a[:, 0].min() - margin,
        a[:, 1].min() - margin,
        a[:, 2].max() + margin,
        a[:, 3].max() + margin,
    )


@dataclass
class Aligned:
    xml: str
    map_name: str
    bounds: Bounds
    best: search.Candidate
    second_gap: float
    geoms: list[BaseGeometry]
    attrs: list[dict[str, str]]
    seconds: float


def align_xml(
    m: parcels.MapXml,
    city: City,
    dem_path: str | Path,
    bounds: Bounds,
    theta_range: float = 10.0,
) -> Aligned:
    t0 = time.time()
    unknown = any(s.attrs.get("方位不明フラグ") == "true" for s in m.sheets)
    thetas = np.arange(-180, 180, 2.0) if unknown else np.arange(-theta_range, theta_range + 0.5)
    scales = np.array([0.97, 0.98, 0.99, 1.0, 1.01, 1.02, 1.03])

    ref = reference.build(dem_path, bounds, city.public)
    local = [p.geometry for p in m.parcels]
    bd = search.sample_boundary(local)
    it = search.sample_interior(local)
    cands = search.global_search(
        bd, ref.field, thetas, scales, interior=it, penalty=ref.penalty, top_k=5
    )
    refined = sorted(
        (
            search.refine(
                bd, ref.field, c.transform, it, ref.penalty,
                theta_bounds=(thetas.min() - 2, thetas.max() + 2),
                scale_bounds=(scales.min() - 0.01, scales.max() + 0.01),
            )
            for c in cands
        ),
        key=lambda c: c.score + c.overlap,
    )  # fmt: skip
    best = refined[0]
    gap = (
        (refined[1].score + refined[1].overlap) - (best.score + best.overlap)
        if len(refined) > 1
        else float("inf")
    )
    params = best.transform.affine_params()
    return Aligned(
        m.file, m.header.get("地図名", ""), bounds, best, float(gap),
        [affine_transform(g, params) for g in local], [p.attrs for p in m.parcels],
        time.time() - t0,
    )  # fmt: skip
