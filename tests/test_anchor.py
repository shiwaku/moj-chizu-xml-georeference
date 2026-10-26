import numpy as np

from moj_georef.anchor import honban, robust_fit
from moj_georef.search import Similarity


def test_robust_fit_recovers_similarity_with_outliers():
    rng = np.random.default_rng(0)
    src = rng.uniform(-200, 200, size=(40, 2))
    truth = Similarity(36000.0, -185000.0, 12.0, 0.97, (0.0, 0.0))
    dst = truth.apply(src) + rng.normal(0, 1.0, size=src.shape)
    dst[:5] += 80.0  # 誤ったジオコード
    fit = robust_fit(src, dst)
    t = fit.transform
    assert t is not None
    assert abs(t.theta_deg - 12.0) < 0.5 and abs(t.scale - 0.97) < 0.01
    assert not fit.inliers[:5].any()
    assert fit.rmse < 2.0


def test_robust_fit_needs_three_points():
    fit = robust_fit(np.zeros((2, 2)), np.zeros((2, 2)))
    assert fit.transform is None


def test_honban():
    assert honban("179-2") == 179
    assert honban("別図-1") is None
