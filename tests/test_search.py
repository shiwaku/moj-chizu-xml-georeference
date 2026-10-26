import numpy as np
import pytest
from affine import Affine
from rasterio.features import rasterize
from shapely.affinity import affine_transform
from shapely.geometry import LineString, box

from moj_georef.dem import Grid, distance_to, oriented_distances, sheet_2500, sheets_2500
from moj_georef.search import (
    DistanceField,
    Similarity,
    global_search,
    refine,
    sample_boundary,
)


def _lots(seed: int = 0) -> list:
    """不規則な幅の区画（周期性があると平行移動が一意に決まらないため）。"""
    rng = np.random.default_rng(seed)
    lots = []
    y = 0.0
    for _ in range(6):
        h = rng.uniform(12, 30)
        x = 0.0
        for _ in range(7):
            w = rng.uniform(10, 25)
            lots.append(box(x, y, x + w, y + h))
            x += w + rng.uniform(0, 6)
        y += h + rng.uniform(4, 8)
    return lots


@pytest.mark.parametrize("n_bins", [1, 6])
def test_recovers_known_similarity(n_bins):
    truth = Similarity(1250.0, -830.0, 3.0, 1.01, (80.0, 70.0))
    placed = [affine_transform(g, truth.affine_params()) for g in _lots()]

    res = 0.5
    tf = Affine(res, 0, 1000, 0, -res, -600)
    shape = (int(500 / res), int(500 / res))
    # 線分ごとに向きを焼き込んだエッジ画像
    segs = []
    for g in placed:
        c = list(g.exterior.coords)
        for a, b in zip(c[:-1], c[1:], strict=True):
            ang = np.degrees(np.arctan2(b[1] - a[1], b[0] - a[0])) % 180
            segs.append((LineString([a, b]).buffer(0.3), ang))
    angle = rasterize(segs, out_shape=shape, transform=tf, fill=-1.0, dtype="float32")
    edges = Grid(angle >= 0, tf)
    if n_bins == 1:
        field = DistanceField([distance_to(edges, 10.0)], 10.0)
    else:
        field = DistanceField(oriented_distances(edges, Grid(angle, tf), 6, 30.0, 10.0), 10.0)

    boundary = sample_boundary(_lots())
    cands = global_search(
        boundary, field, thetas=np.arange(-5, 6, 1.0), scales=np.array([0.99, 1.0, 1.01])
    )
    best = refine(boundary, field, cands[0].transform)
    t = best.transform
    assert best.score < 0.5
    assert abs(t.theta_deg - 3.0) < 0.2
    assert abs(t.scale - 1.01) < 0.003
    # 中心点の写像先が一致すること
    got = t.apply(np.array([truth.center]))[0]
    assert np.hypot(*(got - [truth.tx, truth.ty])) < 0.5


def test_sheet_2500():
    assert sheet_2500(36945, -185435) == "QE193"
    assert sheets_2500((36500, -186200, 37500, -185000)) == ["QE193", "QE291"]


def test_distance_to_accepts_integer_edges():
    tf = Affine(1, 0, 0, 0, -1, 0)
    edges = np.zeros((5, 5), dtype=np.int64)
    edges[2, 2] = 1
    d = distance_to(Grid(edges, tf), 10.0).array
    assert d[2, 2] == 0 and d[2, 3] == 1
