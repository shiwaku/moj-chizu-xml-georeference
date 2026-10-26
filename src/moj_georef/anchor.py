"""対応点（地番のジオコード結果など）による相似変換の推定。

梅原ほか（2024）「登記所備付地図データの公共座標系への自動変換技術に関する研究」の手順に沿う:
エリア分割 → 本番ごとの重心と対応点の二乗誤差最小の相似変換 → 外れ値除去（Q3 + 1.5 IQR）して再推定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from shapely import unary_union
from shapely.geometry.base import BaseGeometry

from moj_georef.parcels import Parcel
from moj_georef.search import Similarity

_HONBAN = re.compile(r"^(\d+)")


def honban(chiban: str) -> int | None:
    m = _HONBAN.match(chiban)
    return int(m[1]) if m else None


def split_areas(parcels: list[Parcel], gap: float = 15.0) -> list[list[Parcel]]:
    """gap [m] 以内に近づく筆をまとめてエリアにする（離れた飛び地は別の座標系の可能性がある）。"""
    if not parcels:
        return []
    merged = unary_union([p.geometry.buffer(gap / 2) for p in parcels])
    parts = list(getattr(merged, "geoms", [merged]))
    out: list[list[Parcel]] = [[] for _ in parts]
    for p in parcels:
        pt = p.geometry.representative_point()
        for i, part in enumerate(parts):
            if part.contains(pt):
                out[i].append(p)
                break
    return [a for a in out if a]


def fit_similarity(src: np.ndarray, dst: np.ndarray, fix_scale: bool = False) -> Similarity:
    """src → dst の最小二乗の相似変換（Umeyama 法、反転なし）。fix_scale なら縮尺 1 の剛体変換。"""
    mu_s, mu_d = src.mean(axis=0), dst.mean(axis=0)
    s0, d0 = src - mu_s, dst - mu_d
    cov = d0.T @ s0 / len(src)
    u, sig, vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(u @ vt))
    r = u @ np.diag([1.0, d]) @ vt
    var = (s0**2).sum() / len(src)
    scale = 1.0 if fix_scale else (sig * np.array([1.0, d])).sum() / var
    theta = float(np.degrees(np.arctan2(r[1, 0], r[0, 0])))
    return Similarity(float(mu_d[0]), float(mu_d[1]), theta, float(scale), tuple(mu_s))


@dataclass
class AnchorFit:
    transform: Similarity | None
    inliers: np.ndarray  # bool
    residuals: np.ndarray  # [m]（全対応点、最終の変換で）
    n_anchors: int

    @property
    def rmse(self) -> float:
        r = self.residuals[self.inliers]
        return float(np.sqrt((r**2).mean())) if len(r) else float("nan")


def robust_fit(
    src: np.ndarray,
    dst: np.ndarray,
    min_points: int = 3,
    rounds: int = 3,
    fix_scale: bool = False,
) -> AnchorFit:
    n = len(src)
    if n < min_points:
        return AnchorFit(None, np.zeros(n, bool), np.full(n, np.nan), n)
    inl = np.ones(n, bool)
    t = fit_similarity(src, dst, fix_scale)
    for _ in range(rounds):
        res = np.hypot(*(t.apply(src) - dst).T)
        q1, q3 = np.percentile(res[inl], [25, 75])
        new = res <= q3 + 1.5 * (q3 - q1)
        if new.sum() < min_points or (new == inl).all():
            break
        inl = new
        t = fit_similarity(src[inl], dst[inl], fix_scale)
    res = np.hypot(*(t.apply(src) - dst).T)
    return AnchorFit(t, inl, res, n)


def honban_centroids(parcels: list[Parcel]) -> dict[tuple[str, int], BaseGeometry]:
    """(大字名, 本番) ごとに、その本番を持つ筆をまとめた重心。"""
    groups: dict[tuple[str, int], list[BaseGeometry]] = {}
    for p in parcels:
        h = honban(p.attrs.get("地番", ""))
        oaza = p.attrs.get("大字名", "").strip().removeprefix("大字")
        if h is not None and oaza:
            groups.setdefault((oaza, h), []).append(p.geometry)
    return {k: unary_union(v).centroid for k, v in groups.items()}


def merge_duplicates(
    src: np.ndarray, dst: np.ndarray, tol: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """同じ座標（tol [m] 以内）に返ったジオコード結果を1点にまとめ、対応する重心は平均する。

    代表点で丸められたジオコード結果が多いと、点の広がりが小さくなり縮尺が縮んで推定されるため。
    """
    keys = np.round(dst / tol).astype(int)
    _, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = inv.ravel()
    n = inv.max() + 1 if len(inv) else 0
    s = np.zeros((n, 2))
    d = np.zeros((n, 2))
    np.add.at(s, inv, src)
    np.add.at(d, inv, dst)
    cnt = np.bincount(inv, minlength=n)[:, None]
    return s / cnt, d / cnt
