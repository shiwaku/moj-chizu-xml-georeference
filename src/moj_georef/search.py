"""相似変換（平行移動・回転・縮尺）の探索と精密化。

評価関数は向き付きチャンファー距離:
筆界のサンプル点ごとに「向きが近い（±tolerance）参照エッジまでの距離」を取り、その平均を使う。
向きを条件にすると、森林の雑音のような向きのばらばらなエッジに引き寄せられにくくなる。

回転・縮尺の組ごとに、向きの区分ごとに点群をテンプレート画像にして距離画像と FFT で
相互相関を取り、全平行移動のスコアを一度に計算する。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage, optimize, signal
from shapely.geometry.base import BaseGeometry

from moj_georef.dem import Grid


@dataclass(frozen=True)
class Similarity:
    """p' = s * R(theta) * (p - center) + (tx, ty)"""

    tx: float
    ty: float
    theta_deg: float
    scale: float
    center: tuple[float, float]

    def apply(self, xy: np.ndarray) -> np.ndarray:
        t = np.radians(self.theta_deg)
        c, s = np.cos(t), np.sin(t)
        p = xy - np.asarray(self.center)
        rot = np.column_stack([c * p[:, 0] - s * p[:, 1], s * p[:, 0] + c * p[:, 1]])
        return self.scale * rot + np.array([self.tx, self.ty])

    def affine_params(self) -> tuple[float, float, float, float, float, float]:
        """shapely.affinity.affine_transform 用の [a, b, d, e, xoff, yoff]。"""
        t = np.radians(self.theta_deg)
        a, b = self.scale * np.cos(t), -self.scale * np.sin(t)
        d, e = self.scale * np.sin(t), self.scale * np.cos(t)
        cx, cy = self.center
        return (a, b, d, e, self.tx - a * cx - b * cy, self.ty - d * cx - e * cy)


@dataclass
class Candidate:
    transform: Similarity
    score: float  # 平均距離 [m]（小さいほど良い）
    overlap: float = 0.0  # 筆の内部点での penalty の平均


@dataclass
class DistanceField:
    """向きの区分ごとの距離画像。grids[k] は向き k*180/n 付近のエッジまでの距離。"""

    grids: list[Grid]
    max_dist: float

    @property
    def n_bins(self) -> int:
        return len(self.grids)

    def bin_of(self, angle_deg: np.ndarray) -> np.ndarray:
        width = 180.0 / self.n_bins
        return (np.round(np.mod(angle_deg, 180.0) / width).astype(int)) % self.n_bins


@dataclass
class Boundary:
    xy: np.ndarray  # (N, 2)
    angle: np.ndarray  # (N,) 線の向き [deg]（0〜180、東=0、反時計回り）


def sample_boundary(geoms: list[BaseGeometry], step: float = 1.0) -> Boundary:
    """筆界線上に step [m] 間隔で点と線の向きを取る（共有境界の重複は除く）。"""
    pts: list[tuple[float, float]] = []
    angles: list[float] = []
    for g in geoms:
        line = g.boundary
        n = max(int(line.length // step), 1)
        for i in range(n):
            a = line.interpolate(i * step).coords[0]
            b = line.interpolate(min(i * step + 0.5, line.length)).coords[0]
            pts.append(a)
            angles.append(np.degrees(np.arctan2(b[1] - a[1], b[0] - a[0])) % 180.0)
    xy = np.round(np.asarray(pts), 1)
    _, idx = np.unique(xy, axis=0, return_index=True)
    return Boundary(xy[idx], np.asarray(angles)[idx])


def sample_interior(geoms: list[BaseGeometry], step: float = 4.0, inset: float = 2.0) -> np.ndarray:
    """筆の内部（境界から inset [m] 内側）に step 間隔で点を取る。"""
    from shapely import contains_xy, unary_union

    area = unary_union(geoms).buffer(-inset)
    if area.is_empty:
        return np.empty((0, 2))
    x0, y0, x1, y1 = area.bounds
    xs, ys = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step))
    xy = np.column_stack([xs.ravel(), ys.ravel()])
    return xy[contains_xy(area, xy[:, 0], xy[:, 1])]


def _downsample(grid: Grid, factor: int) -> Grid:
    if factor == 1:
        return grid
    h, w = (s // factor * factor for s in grid.array.shape)
    a = grid.array[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))
    return Grid(a, grid.transform @ grid.transform.scale(factor))


def _to_pixels(xy: np.ndarray, grid: Grid) -> np.ndarray:
    """地図座標 → (row, col)（小数）。"""
    inv = ~grid.transform
    cols, rows = inv @ (xy[:, 0], xy[:, 1])
    return np.column_stack([rows - 0.5, cols - 0.5])


def _to_rc(t: Similarity, xy: np.ndarray, res: float) -> np.ndarray:
    """変換後の点を (row, col) pixel に換算する（地図座標の原点基準）。"""
    p = t.apply(xy) / res
    return np.column_stack([-p[:, 1], p[:, 0]])


def _correlate(grid: np.ndarray, pts_rc: np.ndarray, origin: np.ndarray) -> np.ndarray:
    """点群（pixel 座標）を grid 上で動かしたときの値の合計。

    戻り値の [r, c] は、点群を (r - origin[0], c - origin[1]) pixel 平行移動したときの合計。
    """
    rc = np.round(pts_rc).astype(int) - origin
    tpl = np.zeros(rc.max(axis=0) + 1, dtype="float32")
    np.add.at(tpl, (rc[:, 0], rc[:, 1]), 1.0)
    return signal.fftconvolve(grid, tpl[::-1, ::-1], mode="valid")


def global_search(
    boundary: Boundary,
    field: DistanceField,
    thetas: np.ndarray,
    scales: np.ndarray,
    interior: np.ndarray | None = None,
    penalty: Grid | None = None,
    penalty_weight: float = 1.0,
    factor: int = 2,
    top_k: int = 5,
) -> list[Candidate]:
    """全域探索。筆の内部点で penalty 画像の平均を取り、penalty_weight 倍して加える。"""
    ds = [_downsample(g, factor) for g in field.grids]
    f = _downsample(penalty, factor) if penalty is not None else None
    tf, res = ds[0].transform, ds[0].res
    center = tuple(boundary.xy.mean(axis=0))
    n = len(boundary.xy)
    use_penalty = f is not None and interior is not None and len(interior) > 0
    results: list[Candidate] = []
    for scale in scales:
        for theta in thetas:
            base = Similarity(0.0, 0.0, float(theta), float(scale), center)

            rc = _to_rc(base, boundary.xy, res)
            pts = [rc] + ([_to_rc(base, interior, res)] if use_penalty else [])
            # すべての点群で共通の原点を使い、相関結果の添字を揃える
            origin = np.floor(np.vstack(pts).min(axis=0)).astype(int)
            size = np.round(np.vstack(pts)).astype(int).max(axis=0) - origin + 1
            out_shape = np.array(ds[0].array.shape) - size + 1
            if (out_shape <= 0).any():
                continue
            # 区分ごとにテンプレートの大きさが違うので、相関結果は out_shape に切り揃える
            bins = field.bin_of(boundary.angle + theta)
            cost = np.zeros(out_shape, dtype="float64")
            for k in range(field.n_bins):
                sel = rc[bins == k]
                if len(sel):
                    cost += _correlate(ds[k].array, sel, origin)[: out_shape[0], : out_shape[1]]
            cost /= n
            if use_penalty:
                ir = pts[1]
                ov = _correlate(f.array, ir, origin)[: out_shape[0], : out_shape[1]] / len(ir)
            else:
                ov = np.zeros_like(cost)
            total = cost + penalty_weight * ov
            mins = total == ndimage.minimum_filter(total, size=int(30 / res) | 1)
            for r, c in zip(*np.nonzero(mins), strict=True):
                # 点群を (r - origin) pixel 動かす → 地図座標の平行移動
                gx, gy = tf @ (c - origin[1] + 0.5, r - origin[0] + 0.5)
                cand = Similarity(gx, gy, float(theta), float(scale), center)
                results.append(Candidate(cand, float(cost[r, c]), float(ov[r, c])))
    results.sort(key=lambda c: c.score + penalty_weight * c.overlap)
    # 平行移動が近い候補（30m 以内）は最良のものだけ残す
    picked: list[Candidate] = []
    for cand in results:
        t = cand.transform
        if all(np.hypot(t.tx - p.transform.tx, t.ty - p.transform.ty) > 30 for p in picked):
            picked.append(cand)
        if len(picked) >= top_k:
            break
    return picked


def sample_grid(grid: Grid, xy: np.ndarray, fill: float) -> np.ndarray:
    rc = _to_pixels(xy, grid)
    return ndimage.map_coordinates(grid.array, rc.T, order=1, mode="constant", cval=fill)


def chamfer(field: DistanceField, boundary: Boundary, t: Similarity) -> float:
    xy = t.apply(boundary.xy)
    bins = field.bin_of(boundary.angle + t.theta_deg)
    total = 0.0
    for k in range(field.n_bins):
        sel = bins == k
        if sel.any():
            total += sample_grid(field.grids[k], xy[sel], field.max_dist).sum()
    return total / len(xy)


def refine(
    boundary: Boundary,
    field: DistanceField,
    init: Similarity,
    interior: np.ndarray | None = None,
    penalty: Grid | None = None,
    penalty_weight: float = 1.0,
    max_shift: float = 20.0,
    theta_bounds: tuple[float, float] = (-180.0, 180.0),
    scale_bounds: tuple[float, float] = (0.95, 1.05),
) -> Candidate:
    """Nelder-Mead で (tx, ty, theta, scale) を精密化する。

    範囲制限がないと、縮尺を極端に小さくして筆界を1本のエッジに重ねる解に落ちるため、
    平行移動（初期値から max_shift [m] 以内）・回転・縮尺に上下限を付ける。
    """

    def build(v: np.ndarray) -> Similarity:
        return Similarity(v[0], v[1], v[2], v[3], init.center)

    def overlap(t: Similarity) -> float:
        if penalty is None or interior is None or not len(interior):
            return 0.0
        return float(sample_grid(penalty, t.apply(interior), 0.0).mean())

    def cost(v: np.ndarray) -> float:
        t = build(v)
        return chamfer(field, boundary, t) + penalty_weight * overlap(t)

    x0 = np.array([init.tx, init.ty, init.theta_deg, init.scale])
    step = np.array([2.0, 2.0, 0.5, 0.005])
    simplex = np.vstack([x0] + [x0 + np.diag(step)[i] for i in range(4)])
    res = optimize.minimize(
        cost,
        x0,
        method="Nelder-Mead",
        bounds=[
            (init.tx - max_shift, init.tx + max_shift),
            (init.ty - max_shift, init.ty + max_shift),
            theta_bounds,
            scale_bounds,
        ],
        options={"initial_simplex": simplex, "xatol": 0.01, "fatol": 1e-4, "maxiter": 2000},
    )
    t = build(res.x)
    return Candidate(t, chamfer(field, boundary, t), overlap(t))
