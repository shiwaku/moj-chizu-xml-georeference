"""地番のジオコーディング（国土地理院 住所検索）。

住居表示を実施していない地域では、住所の番地＝地番の本番なので、
「市区町村＋大字＋本番」を引くと番地単位の代表点が得られる。

- 返ってきた住所が問い合わせた番地と一致しない場合（大字の代表点などが返った場合）は捨てる。
- 結果は JSON にキャッシュし、問い合わせの間隔を空ける（公開サービスに負荷をかけないため）。
"""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import requests

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"


def _norm(s: str) -> str:
    """全角数字などを半角にし、空白を除く。"""
    return unicodedata.normalize("NFKC", s).replace(" ", "")


@dataclass(frozen=True)
class GeocodeResult:
    query: str
    title: str
    lon: float
    lat: float


class GsiGeocoder:
    def __init__(self, cache_path: Path, interval: float = 0.5):
        self.cache_path = cache_path
        self.interval = interval
        self._last = 0.0
        self.cache: dict[str, list] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    def _search(self, query: str) -> list:
        if query in self.cache:
            return self.cache[query]
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        res = requests.get(GSI_URL, params={"q": query}, timeout=30)
        self._last = time.time()
        res.raise_for_status()
        self.cache[query] = res.json()
        return self.cache[query]

    def banchi(self, pref_city: str, oaza: str, number: int) -> GeocodeResult | None:
        """`広島県安芸郡熊野町` + `柿迫` + 179 → 「…柿迫179番地」の点。一致しなければ None。"""
        query = f"{pref_city}{oaza}{number}"
        expected = _norm(f"{oaza}{number}番地")
        for f in self._search(query):
            title = f["properties"]["title"]
            if _norm(title).endswith(expected):
                lon, lat = f["geometry"]["coordinates"]
                return GeocodeResult(query, title, lon, lat)
        return None
