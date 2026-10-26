"""地図XML 1ファイル分のメタ情報（座標系・地図分類・筆数など）を集計する。

形状は読まずに属性だけを iterparse で拾うので、県全体の走査にも使える。
"""

from __future__ import annotations

import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from xml.etree.ElementTree import iterparse

ARBITRARY_CRS = "任意座標系"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class XmlSummary:
    file: str
    map_name: str = ""
    city_code: str = ""
    city_name: str = ""
    crs: str = ""
    datum: str = ""
    n_parcels: int = 0
    n_sheets: int = 0
    oaza: Counter[str] = field(default_factory=Counter)
    coord_kind: Counter[str] = field(default_factory=Counter)
    map_class: Counter[str] = field(default_factory=Counter)
    map_type: Counter[str] = field(default_factory=Counter)
    scale: Counter[str] = field(default_factory=Counter)
    years: list[int] = field(default_factory=list)

    @property
    def is_arbitrary(self) -> bool:
        return self.crs == ARBITRARY_CRS

    def to_row(self) -> dict[str, object]:
        def top(c: Counter[str]) -> str:
            return ";".join(f"{k}:{v}" for k, v in c.most_common())

        return {
            "file": self.file,
            "city_code": self.city_code,
            "city_name": self.city_name,
            "map_name": self.map_name,
            "crs": self.crs,
            "datum": self.datum,
            "n_parcels": self.n_parcels,
            "n_sheets": self.n_sheets,
            "oaza": top(self.oaza),
            "coord_kind": top(self.coord_kind),
            "map_class": top(self.map_class),
            "map_type": top(self.map_type),
            "scale": top(self.scale),
            "year_min": min(self.years, default=""),
            "year_max": max(self.years, default=""),
        }


_HEADER_FIELDS = {
    "地図名": "map_name",
    "市区町村コード": "city_code",
    "市区町村名": "city_name",
    "座標系": "crs",
    "測地系判別": "datum",
}


def summarize(fp: IO[bytes], name: str) -> XmlSummary:
    s = XmlSummary(file=name)
    path: list[str] = []
    for event, elem in iterparse(fp, events=("start", "end")):
        tag = _local(elem.tag)
        if event == "start":
            path.append(tag)
            continue
        path.pop()
        parent = path[-1] if path else ""
        text = (elem.text or "").strip()
        if parent == "地図" and tag in _HEADER_FIELDS:
            setattr(s, _HEADER_FIELDS[tag], text)
        elif parent == "筆":
            if tag == "大字名":
                s.oaza[text] += 1
            elif tag == "座標値種別":
                s.coord_kind[text] += 1
        elif tag == "筆" and parent == "主題属性":
            s.n_parcels += 1
            elem.clear()
        elif parent == "図郭":
            if tag == "地図分類":
                s.map_class[text] += 1
            elif tag == "地図種類":
                s.map_type[text] += 1
            elif tag == "縮尺分母":
                s.scale[text] += 1
        elif tag == "年" and parent == "地図作成年月日" and text.isdigit():
            s.years.append(int(text))
        elif tag == "図郭":
            s.n_sheets += 1
            elem.clear()
        elif tag in ("GM_Point", "GM_Curve", "GM_Surface", "筆界点", "筆界線"):
            elem.clear()
    return s


def iter_city_zip(path: Path) -> Iterator[XmlSummary]:
    """市区町村単位の zip（中に地区ごとの zip → xml が入っている）を走査する。"""
    with zipfile.ZipFile(path) as outer:
        for inner_name in sorted(outer.namelist()):
            if not inner_name.endswith(".zip"):
                continue
            with outer.open(inner_name) as raw, zipfile.ZipFile(raw) as inner:
                for xml_name in inner.namelist():
                    if xml_name.endswith(".xml"):
                        with inner.open(xml_name) as fp:
                            yield summarize(fp, xml_name)
