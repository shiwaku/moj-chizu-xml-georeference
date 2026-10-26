"""国土地理院 基盤地図情報（基本項目）ベクトルタイル（experimental_fgd）の取得。

https://cyberjapandata.gsi.go.jp/xyz/experimental_fgd/{z}/{x}/{y}.geojson
地図情報レベル 2500 相当の道路縁（RdEdg）・建築物外周線（BldL）・水涯線（WL）などを含む。
タイルはローカルにキャッシュし、取得の間隔を空ける。
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterable
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import LineString, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, transform, unary_union

from moj_georef.dem import Bounds

URL = "https://cyberjapandata.gsi.go.jp/xyz/experimental_fgd/{z}/{x}/{y}.geojson"
ZOOM = 18


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


class FgdTiles:
    def __init__(self, cache_dir: Path, epsg: int = 6671, interval: float = 0.2):
        self.cache_dir = cache_dir
        self.to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:6668", always_xy=True)
        self.to_xy = Transformer.from_crs("EPSG:6668", f"EPSG:{epsg}", always_xy=True)
        self.interval = interval
        self._last = 0.0

    def _tile(self, x: int, y: int) -> list[dict]:
        path = self.cache_dir / str(ZOOM) / str(x) / f"{y}.geojson"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8")).get("features", [])
            except json.JSONDecodeError:
                pass  # 壊れたキャッシュは取り直す
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        res = requests.get(URL.format(z=ZOOM, x=x, y=y), timeout=60)
        self._last = time.time()
        # 404 はデータのないタイル（海など）なので空として扱う
        text = res.text if res.ok else '{"features": []}'
        path.parent.mkdir(parents=True, exist_ok=True)
        # 複数プロセスが同じタイルを書いても壊れないよう、一時ファイルから置き換える
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return json.loads(text).get("features", [])

    def features(self, bounds: Bounds, classes: Iterable[str]) -> dict[str, list[BaseGeometry]]:
        """範囲内の地物を class ごとに平面直角座標で返す（タイル境界での重複は残る）。"""
        xmin, ymin, xmax, ymax = bounds
        lon0, lat0 = self.to_ll.transform(xmin, ymin)
        lon1, lat1 = self.to_ll.transform(xmax, ymax)
        x0, y1 = _tile_xy(lon0, lat0, ZOOM)
        x1, y0 = _tile_xy(lon1, lat1, ZOOM)
        wanted = set(classes)
        out: dict[str, list[BaseGeometry]] = {c: [] for c in wanted}
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                for f in self._tile(tx, ty):
                    c = f["properties"].get("class")
                    if c in wanted and f.get("geometry"):
                        out[c].append(transform(self.to_xy.transform, shape(f["geometry"])))
        return out


def building_polygons(lines: list[BaseGeometry]) -> list[BaseGeometry]:
    """建築物外周線（線）をポリゴンにする。タイル境界で切れた線は閉じないので落ちる。"""
    segs: list[LineString] = []
    for g in lines:
        segs += list(getattr(g, "geoms", [g]))
    return [p for p in polygonize(unary_union(segs)) if p.area >= 10]
