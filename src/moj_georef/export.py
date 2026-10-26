"""GeoPackage への書き出し。

Python から GDAL バインディングを使わず、GeoJSON を経由して ogr2ogr で GPKG に変換する。
ogr2ogr の場所は環境変数 `GDAL_BIN`（ディレクトリ）で指定でき、未指定なら PATH から探す。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

Feature = tuple[Mapping[str, object], BaseGeometry]


def gdal_tool(name: str) -> str:
    bin_dir = os.environ.get("GDAL_BIN")
    if bin_dir:
        exe = Path(bin_dir) / (name + (".exe" if os.name == "nt" else ""))
        if exe.exists():
            return str(exe)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} が見つからない。環境変数 GDAL_BIN を設定してください")
    return found


def write_layer(
    gpkg: Path, layer: str, features: Iterable[Feature], epsg: int, append: bool = False
) -> int:
    """features を gpkg の layer に書き出す。座標はそのまま、CRS には epsg を付ける。"""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": dict(props), "geometry": mapping(geom)}
            for props, geom in features
        ],
    }
    if not fc["features"]:
        return 0
    gpkg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{layer}.geojson"
        src.write_text(json.dumps(fc, ensure_ascii=False, default=_json_default), encoding="utf-8")
        cmd = [gdal_tool("ogr2ogr"), "-f", "GPKG", "-nln", layer, "-a_srs", f"EPSG:{epsg}"]
        if gpkg.exists():
            cmd.append("-update")
            cmd.append("-append" if append else "-overwrite")
        cmd += [str(gpkg), str(src)]
        subprocess.run(cmd, check=True, capture_output=True)
    return len(fc["features"])


def _json_default(o: object) -> object:
    """numpy の数値型など、json が直接扱えない値を変換する。"""
    if hasattr(o, "item"):
        return o.item()  # type: ignore[union-attr]
    raise TypeError(f"{type(o).__name__} は JSON にできない")
