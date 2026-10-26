"""地図XML を筆ポリゴンに変換する。

- 座標は (x=東方向=zmn:Y, y=北方向=zmn:X) に入れ替えて返す。
- 任意座標系の地区も局所座標のまま返す（位置合わせ前の入力にするため）。
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from xml.etree.ElementTree import Element, parse

from shapely import STRtree, unary_union
from shapely.geometry import Polygon
from shapely.validation import make_valid

ZMN = "{http://www.moj.go.jp/MINJI/tizuzumen}"
TIZU = "{http://www.moj.go.jp/MINJI/tizuxml}"

_PUBLIC_CRS = re.compile(r"公共座標(\d+)系")

Coord = tuple[float, float]


def crs_to_epsg(crs: str) -> int | None:
    """`公共座標3系` → 6671（JGD2011 平面直角座標系）。任意座標系は None。"""
    m = _PUBLIC_CRS.fullmatch(crs)
    return 6668 + int(m[1]) if m else None


@dataclass
class Parcel:
    id: str
    attrs: dict[str, str]
    geometry: Polygon


@dataclass
class Sheet:
    attrs: dict[str, str]
    geometry: Polygon
    parcel_ids: list[str] = field(default_factory=list)


@dataclass
class MapXml:
    file: str
    header: dict[str, str]
    parcels: list[Parcel]
    sheets: list[Sheet]

    @property
    def epsg(self) -> int | None:
        return crs_to_epsg(self.header.get("座標系", ""))


def _xy(pos: Element) -> Coord:
    return float(pos.findtext(f"{ZMN}Y", "nan")), float(pos.findtext(f"{ZMN}X", "nan"))


def _read_points(spatial: Element) -> dict[str, Coord]:
    return {
        p.get("id", ""): _xy(p.find(f".//{ZMN}DirectPosition"))  # type: ignore[arg-type]
        for p in spatial.iter(f"{ZMN}GM_Point")
    }


def _read_curves(spatial: Element, points: dict[str, Coord]) -> dict[str, list[Coord]]:
    curves: dict[str, list[Coord]] = {}
    for c in spatial.iter(f"{ZMN}GM_Curve"):
        coords: list[Coord] = []
        for col in c.iter(f"{ZMN}GM_PointArray.column"):
            direct = col.find(f"{ZMN}GM_Position.direct")
            if direct is not None:
                coords.append(_xy(direct))
                continue
            ref = col.find(f".//{ZMN}GM_PointRef.point")
            if ref is not None:
                coords.append(points[ref.get("idref", "")])
        if c.findtext(f"{ZMN}GM_OrientablePrimitive.orientation", "+") == "-":
            coords.reverse()
        curves[c.get("id", "")] = coords
    return curves


def _ring(ring: Element, curves: dict[str, list[Coord]]) -> list[Coord]:
    """複数の曲線をつないで閉じたリングにする（つなぎ目の重複点は除く）。"""
    out: list[Coord] = []
    for gen in ring.iter(f"{ZMN}GM_CompositeCurve.generator"):
        seg = curves[gen.get("idref", "")]
        if out and seg and out[-1] != seg[0] and out[-1] == seg[-1]:
            seg = seg[::-1]
        out.extend(seg[1:] if out and seg and out[-1] == seg[0] else seg)
    return out


def _read_surfaces(spatial: Element, curves: dict[str, list[Coord]]) -> dict[str, Polygon]:
    surfaces: dict[str, Polygon] = {}
    for s in spatial.iter(f"{ZMN}GM_Surface"):
        ext = s.find(f".//{ZMN}GM_SurfaceBoundary.exterior/{ZMN}GM_Ring")
        if ext is None:
            continue
        interiors = s.iterfind(f".//{ZMN}GM_SurfaceBoundary.interior/{ZMN}GM_Ring")
        holes = [_ring(r, curves) for r in interiors]
        poly = Polygon(_ring(ext, curves), [h for h in holes if len(h) >= 3])
        if not poly.is_valid:
            fixed = make_valid(poly)
            if fixed.geom_type == "Polygon":
                poly = fixed
            else:  # 自己交差で複数に分かれた場合は最大のものを採用
                poly = max(getattr(fixed, "geoms", [poly]), key=lambda g: g.area)
                if poly.geom_type != "Polygon":
                    continue
        surfaces[s.get("id", "")] = poly
    return surfaces


def _corner(el: Element, name: str) -> Coord:
    return _xy(el.find(f"{TIZU}{name}"))  # type: ignore[arg-type]


def read(fp: IO[bytes], name: str) -> MapXml:
    root = parse(fp).getroot()
    header = {
        k: root.findtext(f"{TIZU}{k}", "")
        for k in ("地図名", "市区町村コード", "市区町村名", "座標系", "測地系判別")
    }
    spatial = root.find(f"{TIZU}空間属性")
    if spatial is None:
        return MapXml(name, header, [], [])
    points = _read_points(spatial)
    curves = _read_curves(spatial, points)
    surfaces = _read_surfaces(spatial, curves)

    parcels: list[Parcel] = []
    theme = root.find(f"{TIZU}主題属性")
    for h in theme.iterfind(f"{TIZU}筆") if theme is not None else []:
        shape = h.find(f"{TIZU}形状")
        geom = surfaces.get(shape.get("idref", "")) if shape is not None else None
        if geom is None:
            continue
        attrs = {_local(c.tag): (c.text or "").strip() for c in h if _local(c.tag) != "形状"}
        parcels.append(Parcel(h.get("id", ""), attrs, geom))

    sheets: list[Sheet] = []
    for z in root.iterfind(f"{TIZU}図郭"):
        corners = [_corner(z, n) for n in ("左下座標", "右下座標", "右上座標", "左上座標")]
        attrs = {
            _local(c.tag): (c.text or "").strip()
            for c in z
            if len(c) == 0 and _local(c.tag) != "筆参照"
        }
        date = z.find(f"{TIZU}地図作成年月日")
        if date is not None:
            attrs["作成年"] = date.findtext(f"{TIZU}年", "")
        refs = [r.get("idref", "") for r in z.iterfind(f"{TIZU}筆参照")]
        sheets.append(Sheet(attrs, Polygon(corners), refs))

    return MapXml(name, header, parcels, sheets)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_city_zip(path: Path) -> Iterator[MapXml]:
    with zipfile.ZipFile(path) as outer:
        for inner_name in sorted(outer.namelist(), key=_natural_key):
            if not inner_name.endswith(".zip"):
                continue
            with outer.open(inner_name) as raw, zipfile.ZipFile(raw) as inner:
                for xml_name in inner.namelist():
                    if xml_name.endswith(".xml"):
                        with inner.open(xml_name) as fp:
                            yield read(fp, xml_name)


def _natural_key(s: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def blocks(parcels: list[Parcel], tol: float = 0.05) -> list[tuple[Polygon, list[str]]]:
    """境界を共有する筆をまとめた塊（ブロック）を返す。位置合わせの単位にする。

    tol [m] だけ膨らませてから結合するので、数 cm の隙間は同じブロックとみなす。
    """
    if not parcels:
        return []
    merged = unary_union([p.geometry.buffer(tol, join_style="mitre") for p in parcels])
    parts = list(getattr(merged, "geoms", [merged]))
    tree = STRtree(parts)
    members: list[list[str]] = [[] for _ in parts]
    for p in parcels:
        idx = tree.query(p.geometry.representative_point(), predicate="intersects")
        if len(idx):
            members[int(idx[0])].append(p.id)
    return [
        (part.buffer(-tol, join_style="mitre"), ids)
        for part, ids in zip(parts, members, strict=True)
        if ids
    ]
