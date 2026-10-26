# moj-chizu-xml-georeference

法務省 登記所備付地図データ（地図XML）のうち、任意座標系の地区を公共座標系へ位置合わせする実験リポジトリ。

設計は [docs/design.md](docs/design.md) を参照。

## セットアップ

```sh
uv sync
```

## 使い方

```sh
# 広島県の地図XMLを走査し、地区ごとの座標系・地図分類などを CSV に出力
uv run moj-georef survey --pref 34 --out data/survey/34.csv

# 容量の小さい市区町村だけ（MB 単位）
uv run moj-georef survey --max-size-mb 100 --out data/survey/34_small.csv
```

```sh
# 市区町村 zip を筆ポリゴンの GPKG にする（公共座標系・任意座標系・ブロック・図郭）
uv run moj-georef prepare data/raw/34307-2400-2026.zip

# 公共座標系の図郭をわざと崩して戻し、位置合わせの精度を測る（正解あり）
uv run moj-georef evaluate data/raw/34307-2400-2026.zip --dem <DEM.tif> --n 12

# 任意座標系の地区を DEM・筆界だけで位置合わせする（大字の範囲から全域探索）
uv run moj-georef align data/raw/34307-2400-2026.zip --dem <DEM.tif>

# 地番ジオコード（国土地理院 住所検索）で粗く合わせ、DEM・筆界・基盤地図情報で細かく合わせる
uv run moj-georef geocode-align data/raw/34307-2400-2026.zip --dem <DEM.tif> --fgd

# 結果を確認する QGIS プロジェクトを作る（QGIS 同梱の Python で実行）
"C:/Program Files/QGIS 4.0.0/bin/python-qgis.bat" qgis/make_review_project.py --out data/qgis/review.qgz ...
```

- ダウンロードしたデータと生成物は `data/` に置かれる（Git 管理外）。
- GPKG の書き出しには `ogr2ogr` を使う。PATH にない場合は環境変数 `GDAL_BIN` に GDAL の bin ディレクトリを指定する
  （例: `C:/Program Files/QGIS 4.0.0/bin`）。
- `--fgd` は国土地理院の基盤地図情報ベクトルタイル（experimental_fgd）を取得して `data/cache/fgd` にキャッシュする。
- DEM は平面直角座標系（地図XMLと同じ系）の GeoTIFF / VRT。広島県の検証では 0.5m DEM（広島県 DoboX の航空レーザ測量成果から作成）を使った。

## 開発

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

## データ

- [G空間情報センター 登記所備付地図データ](https://front.geospatial.jp/moj-chizu-xml-download/)（法務省、利用規約に従う）
