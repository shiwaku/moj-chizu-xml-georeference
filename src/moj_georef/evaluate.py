"""公共座標系の図郭を局所座標系に「わざと崩して」、位置合わせで元に戻せるかを測る。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry.base import BaseGeometry

from moj_georef import parcels, reference, search
from moj_georef.dem import Grid
from moj_georef.reference import PublicParcels, is_pseudo


@dataclass
class Case:
    name: str  # "<xml>#<地図番号>"
    xml: str
    geoms: list[BaseGeometry]  # 正しい位置の筆


@dataclass
class Result:
    case: str
    n_parcels: int
    truth: search.Similarity
    found: search.Similarity
    score: float
    overlap: float
    second_gap: float  # 2位とのスコア差（大きいほど一意）
    error_mean: float  # 筆の重心のずれの平均 [m]
    error_max: float
    seconds: float
    found_geoms: list[BaseGeometry] = field(default_factory=list)  # 結果の位置の筆
    errors: list[float] = field(default_factory=list)  # 筆ごとの重心のずれ [m]
    edges: Grid | None = None  # 参照に使ったエッジ（確認用）


def load_city(zip_path: Path) -> tuple[PublicParcels, list[Case]]:
    geoms: list[BaseGeometry] = []
    keys: list[str] = []
    cases: list[Case] = []
    for m in parcels.iter_city_zip(zip_path):
        if not m.epsg:
            continue
        real = {p.id: p for p in m.parcels if not is_pseudo(p.attrs.get("地番", ""))}
        geoms += [p.geometry for p in real.values()]
        keys += [m.file] * len(real)
        for s in m.sheets:
            gs = [real[i].geometry for i in s.parcel_ids if i in real]
            cases.append(Case(f"{m.file}#{s.attrs.get('地図番号', '')}", m.file, gs))
    return PublicParcels(geoms, keys), cases


def run_case(
    case: Case,
    public: PublicParcels,
    dem_path: str | Path,
    rng: np.random.Generator,
    window: float = 300.0,
    thetas: np.ndarray | None = None,
    scales: np.ndarray | None = None,
) -> Result:
    thetas = np.arange(-6, 6.5, 1.0) if thetas is None else thetas
    scales = np.array([0.98, 0.99, 1.0, 1.01, 1.02]) if scales is None else scales
    t0 = time.time()

    # 正解の変換 truth: 局所座標 → 公共座標。局所座標はその逆で作る。
    xmin, ymin, xmax, ymax = np.array([g.bounds for g in case.geoms]).T
    cx, cy = (xmin.min() + xmax.max()) / 2, (ymin.min() + ymax.max()) / 2
    theta, scale = rng.uniform(-5, 5), rng.uniform(0.98, 1.02)
    local_center = (rng.uniform(-500, 500), rng.uniform(-500, 500))
    truth = search.Similarity(cx, cy, theta, scale, local_center)
    inv = _inverse(truth)
    local = [affine_transform(g, inv) for g in case.geoms]

    bounds = (xmin.min() - window, ymin.min() - window, xmax.max() + window, ymax.max() + window)
    ref = reference.build(dem_path, bounds, public, exclude={case.xml})

    bd = search.sample_boundary(local)
    it = search.sample_interior(local)
    cands = search.global_search(
        bd, ref.field, thetas, scales, interior=it, penalty=ref.penalty, top_k=5
    )
    refined = sorted(
        (
            search.refine(
                bd, ref.field, c.transform, it, ref.penalty,
                theta_bounds=(thetas.min() - 1, thetas.max() + 1),
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
        else np.inf
    )

    true_c = np.array([g.centroid.coords[0] for g in case.geoms])
    local_c = np.array([g.centroid.coords[0] for g in local])
    err = np.hypot(*(best.transform.apply(local_c) - true_c).T)
    found = [affine_transform(g, best.transform.affine_params()) for g in local]
    return Result(
        case.name, len(case.geoms), truth, best.transform, best.score, best.overlap,
        float(gap), float(err.mean()), float(err.max()), time.time() - t0,
        found, err.tolist(), ref.edges,
    )  # fmt: skip


def _inverse(t: search.Similarity) -> list[float]:
    """Similarity の逆変換を shapely の affine_transform 用パラメータで返す。"""
    a, b, d, e, xo, yo = t.affine_params()
    m = np.linalg.inv(np.array([[a, b, xo], [d, e, yo], [0, 0, 1]]))
    return [m[0, 0], m[0, 1], m[1, 0], m[1, 1], m[0, 2], m[1, 2]]
