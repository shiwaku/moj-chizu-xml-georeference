"""G空間情報センター (CKAN) 上の法務省地図XMLデータセットの一覧取得とダウンロード。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests

CKAN_API = "https://www.geospatial.jp/ckan/api/3/action"

# 都道府県コード → CKAN organization 名（最新年度のもの）
PREF_ORGS = {
    "34": "moj-34hiroshima",
}

_ZIP_NAME = re.compile(r"(?P<city>\d{5})-(?P<office>\d{4})-(?P<year>\d{4})\.zip$")


@dataclass(frozen=True)
class Dataset:
    title: str
    url: str
    size: int | None
    city_code: str
    year: int

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]


def parse_resource(title: str, url: str, size: int | None) -> Dataset | None:
    m = _ZIP_NAME.search(url)
    if not m:
        return None
    return Dataset(title, url, size, m["city"], int(m["year"]))


def list_datasets(pref_code: str, latest_only: bool = True) -> list[Dataset]:
    org = PREF_ORGS[pref_code]
    res = requests.get(
        f"{CKAN_API}/package_search",
        params={"fq": f"organization:{org}", "rows": 1000},
        timeout=60,
    )
    res.raise_for_status()
    datasets = [
        ds
        for pkg in res.json()["result"]["results"]
        for r in pkg["resources"]
        if (ds := parse_resource(pkg["title"], r["url"], r.get("size")))
    ]
    if latest_only:
        latest: dict[str, Dataset] = {}
        for ds in datasets:
            if ds.city_code not in latest or ds.year > latest[ds.city_code].year:
                latest[ds.city_code] = ds
        datasets = list(latest.values())
    return sorted(datasets, key=lambda d: d.city_code)


def download(ds: Dataset, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / ds.filename
    if path.exists() and (ds.size is None or path.stat().st_size == ds.size):
        return path
    tmp = path.with_suffix(".part")
    with requests.get(ds.url, stream=True, timeout=300) as res:
        res.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in res.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(path)
    return path
