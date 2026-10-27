---
name: moj-georef
description: 法務省 登記所備付地図データ（地図XML）のうち任意座標系の地区を、平面直角座標系へ自動で位置合わせする。地番ジオコード（国土地理院 住所検索）で粗く合わせ、0.5m DEM・基盤地図情報（道路縁・建物）で細かく合わせて、エリアごとの信頼度を付けたポリゴンを出す。「任意座標の地図XMLを位置合わせして」「公図を公共座標に載せて」「備付地図の任意座標系を変換して」などで使う。QGIS（Mapsuke）の中から moj-chizu-xml-georeference リポジトリを呼ぶ。
---

# 任意座標系の地図XMLを位置合わせする

リポジトリ `C:/Users/yshiw/Documents/GIS/moj-chizu-xml-georeference` の `moj_georef` を QGIS の Python から呼ぶ。
手法と限界はリポジトリの `docs/method.md` にある。必要なライブラリ（rasterio・scipy・shapely・pyproj・requests）は QGIS 同梱の Python に入っているので、パッケージを入れる必要はない。

## 前提

| もの | 場所 |
|---|---|
| 市区町村の地図XML（zip） | `data/raw/<市区町村コード>-2400-<年>.zip`（例: 熊野町 `34307-2400-2026.zip`） |
| 0.5m DEM（広島県、EPSG:6671） | `E:/GIS/pref-hiroshima-dem-2022/hiroshima-dem-2022.tif` |
| GDAL（GPKG の書き出しに使う） | `C:/Program Files/QGIS 4.0.0/bin` を環境変数 `GDAL_BIN` に入れる |

- DEM は広島県の範囲しかない。広島県外の市区町村は DEM がないので、利用者に伝えて止める。
- zip が `data/raw` にない場合は、勝手に探さず利用者に確認する。
- 参照データは国土地理院のもの（住所検索・基盤地図情報ベクトルタイル）だけを使う。OpenStreetMap は使わない。

## 手順

1. **対象を確かめる。** zip 内の任意座標系の XML を一覧して利用者に見せ、どれを合わせるか確認する。

   ```python
   import sys
   from pathlib import Path
   REPO = Path(r"C:/Users/yshiw/Documents/GIS/moj-chizu-xml-georeference")
   if str(REPO / "src") not in sys.path:
       sys.path.insert(0, str(REPO / "src"))
   from moj_georef import align
   city = align.load_city(REPO / "data/raw/34307-2400-2026.zip")
   for m in city.arbitrary:
       print(m.file, m.header.get("地図名"), len(m.parcels))
   ```

   選んだ XML は、実行する前に **ジオコードが効くか** を確かめる。住所検索で番地の位置が引けないと、数分かけて実行しても粗い位置合わせの段階で全部外され、何も出力されない（ログに「wrote」と出ても GPKG は作られない）。本番を4〜20件ほど問い合わせ、番地ごとに **別々の位置** が返るかを見る。`banchi` は町の代表点しか返らないときに None を返す。

   ```python
   from moj_georef import anchor
   from moj_georef.geocode import GsiGeocoder
   m = next(x for x in city.arbitrary if x.file == "34307-2400-17.xml")
   pc = "広島県" + m.header["市区町村名"]
   # 大字と本番は本番処理と同じく honban_centroids から取る（属性の大字名は「大字神田」のように
   # 「大字」が付くことがあり、そのまま問い合わせると1件も引けない）
   keys = sorted(anchor.honban_centroids(m.parcels))
   pick = keys[::max(1, len(keys) // 20)][:20]
   g = GsiGeocoder(REPO / "data/cache/gsi_geocode.json")
   try:
       hits = [(o, n, r.lon, r.lat) for (o, n) in pick if (r := g.banchi(pc, o, n))]
   finally:
       g.save()
   print(f"ヒット {len(hits)}/{len(pick)}  別々の位置 {len({h[2:] for h in hits})}")
   ```

   熊野町（地番がそのまま住所）では引ける（大字神田 14/20・別々の位置11）が、広島市中区・東区ではほとんど引けなかった（舟入南Ｂ 0点、温品町Ｅ 20件中4件・290筆のエリアで別々の位置2つ）。別々の位置がほとんど出ない XML は実行せず、利用者に伝えて別の地区を勧める。

2. **時間がかかることを伝える。** 処理中は QGIS が反応しなくなる。熊野町の XML 1つ（6エリア・229筆）で約2分かかり、XML を増やすとその分長くなる。初めての地区は XML を1つ選んで試すよう勧める。

3. **位置合わせを実行する。** 相対パス（`data/cache` など）はリポジトリ基準なので、作業ディレクトリを移してから呼び、終わったら戻す。ログは標準エラーに出るので受け取って見せる。

   ```python
   import contextlib, io, os
   from moj_georef import cli
   out = REPO / "data/align/mapsuke/34307-2400-17.gpkg"
   out.parent.mkdir(parents=True, exist_ok=True)
   os.environ["GDAL_BIN"] = r"C:/Program Files/QGIS 4.0.0/bin"
   log, cwd = io.StringIO(), os.getcwd()
   try:
       os.chdir(REPO)
       with contextlib.redirect_stderr(log):
           cli.main([
               "geocode-align", "data/raw/34307-2400-2026.zip",
               "--dem", r"E:/GIS/pref-hiroshima-dem-2022/hiroshima-dem-2022.tif",
               "--fgd",
               "--xml", "34307-2400-17.xml",  # 省略すると全ての任意座標系
               "--out", str(out),
           ])
   except SystemExit as e:  # 引数の誤りは argparse が SystemExit で知らせる
       print("引数エラー", e)
   finally:
       os.chdir(cwd)
   print(log.getvalue())
   ```

   - 広島県以外の自治体では `--pref-name`（住所の先頭に付ける都道府県名）と `--epsg`（平面直角座標系）を合わせる。
   - `--fgd` を外すと基盤地図情報を使わない。精度が大きく落ちる（DEM だけでは成功率 5/20、基盤地図情報を加えて 14/20）ので、外さない。

4. **結果をレイヤとして追加する。** 出力 GPKG のレイヤは次の4つ。

   | レイヤ | 中身 |
   |---|---|
   | `aligned` | 細かく合わせた筆ポリゴン（主な結果） |
   | `aligned_coarse` | ジオコードだけで粗く合わせた筆ポリゴン（細かく合わせられなかったエリアもここには出る） |
   | `anchors` | ジオコードで得た番地の点 |
   | `residuals` | 粗い位置合わせの残差（線） |

   `aligned` を `reliable` 属性で色分けする（真: 信頼できる、偽: 要確認）。地番ラベルを付けると確認しやすい。

5. **結果を報告する。** エリア（`area` 属性、`<XML名>#<番号>`）ごとに次を伝える。
   - `reliable`: 1位と2位の評価値の差（`second_gap`）が 1.0 以上なら真。偽のエリアは、別の場所に合っている可能性がある。`second_gap` が NaN のときは2位の候補がなく、判断できない。
   - `theta`（回転 °）、`scale`（縮尺）。同じ地区の信頼できるエリアと回転が大きく違うものは疑う。
   - 信頼できないエリアは、オルソ画像などとの目視確認を勧める。

## 注意

- XML を1つだけ選んで実行すると、全体で実行したときと結果が少し変わることがある。2周目の探索で使う「信頼できたエリアの回転の中央値」が、その XML の中だけで計算されるため。本番は市区町村全体で実行する。
- 位置合わせは相似変換（移動・回転・縮尺）だけで、公図の中の歪みは残る。
- 対応できない地区: 地番に数字がない地区（熊野町 15A05 など）、住所検索で見つからない大字（熊野町 貴船など）、5筆未満の小さいエリア、道路や建物の少ない山林・農地。こうした地区は、合わなかったことをそのまま伝える。
- 住居表示の地区（広島市中区・東区など）では、地番で住所検索しても町の代表点しか返らず、番地の位置が引けない。この方法は地番がそのまま住所になっている町村部（熊野町など）向け。
- 粗い位置合わせには、エリアごとに別々の位置が3つ以上いる。足りないエリアや5筆未満のエリアは、`geocode-align` がログを出さずに外す。全部外れると、エリアごとのログ行が出ず `consensus theta: None` になり、GPKG も作られない。そのときは `two_stage.build_areas(xmls, GsiGeocoder(...), "広島県", 6671, 15.0)` を呼んで、エリアごとの `len(a.keys)`・`a.n_unique`・`a.coarse.transform` を見て原因を確かめる（`cli` には引数を作る関数がないので、既定値 `--gap 15`・`--min-parcels 5`・`--min-unique 3` を直接渡す）。
- 精度の数値（中央値0.58m）は、公共座標系の図郭を崩して戻した評価のもの。任意座標系での精度は、オルソ画像との目視（1m前後）しか根拠がない。利用者に伝えるときは区別する。
- 結果を CS立体図・オルソ画像と重ねて検査線でずれを測るときは、スキル `moj-georef-verify` を使う。
- 結果は実験段階のもので、精度を保証しない。登記や測量の判断に使わないよう伝える。
