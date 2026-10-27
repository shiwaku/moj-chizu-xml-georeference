---
name: moj-georef-verify
description: 任意座標系の地図XMLを位置合わせした結果（moj-georef の出力 GPKG）を、QGIS（Mapsuke）の中で CS立体図・オルソ画像と重ね、オルソ画像上の検査線でずれを測って確かめる。「位置合わせの精度を確かめたい」「オルソ画像と重ねて検証して」「検査点を取りたい」などで使う。位置合わせそのものは moj-georef を使う。
---

# 位置合わせ結果を検証する

`moj-georef` で作った GPKG（レイヤ `aligned` など）を、CS立体図とオルソ画像に重ねて確かめ、手作業で引いた検査線でずれ（m）を集計する。
リポジトリは `C:/Users/yshiw/Documents/GIS/moj-chizu-xml-georeference`（以下 REPO）。

## 前提

| もの | 場所（熊野町の例） |
|---|---|
| 位置合わせ結果 | `REPO/data/align/mapsuke/<名前>.gpkg`（`aligned` に `area`・`reliable`・`second_gap`・`theta`・`scale`・`地番`） |
| CS立体図（作成済み） | 熊野町: `REPO/data/qgis/kumano_csmap.tif`、広島県全体: `E:/GIS/pref-hiroshima-dem-2022/csmap-py/hiroshima-dem-2022-csmap.tif`（どちらも EPSG:6671） |
| オルソ画像（広島県 DoboX、20cm） | `REPO/data/ref/ortho/qe193_2016/03qe193_06_photo.tif`（2016年度）、`qe193_2014/03qe193_01_photo.tif`（2014年度）。図郭 QE193 の1枚だけで、熊野町の大字神田付近しか覆わない |

- まず `processing.run('mapsuke:project_state', {})` でレイヤ ID を確かめる。
- CS立体図は作り直さず、作成済みのものが結果の範囲を覆っているか（`extent().contains(...)`）を確かめて使う。覆っていなければ県全体のものを使う。新しく作る場合だけ `csmap:dem_to_csmap` を使い、DEM は先に範囲で切り出す（県全体の 0.5m DEM は大きすぎる）。
- オルソ画像が結果の範囲を覆っていなければ、そのことを伝え、別の図郭の画像を用意してもらう。勝手に探したりダウンロードしたりしない。

## 手順

1. **重ねる順番。** 上から 検査線 → 位置合わせ結果 → オルソ画像 → CS立体図。CS立体図はレイヤツリーの一番下に入れる。

2. **オルソ画像を読み込む。** DoboX の画像は座標系の情報がなく `.tfw` だけ。図郭番号「03QE193」の先頭 03 は平面直角座標系の第3系なので EPSG:6671 を設定する（QGIS 上のレイヤに設定するだけで、ファイルは書き換えない）。読み込んだら範囲が図郭と合うか確かめる（QE193 は X 36,000〜38,000、Y -186,000〜-184,500）。

   ```python
   from qgis.core import QgsRasterLayer, QgsProject, QgsCoordinateReferenceSystem
   lyr = QgsRasterLayer(r"C:/Users/yshiw/Documents/GIS/moj-chizu-xml-georeference/data/ref/ortho/qe193_2016/03qe193_06_photo.tif", "オルソ画像 2016年度 (QE193)")
   lyr.setCrs(QgsCoordinateReferenceSystem("EPSG:6671"))
   QgsProject.instance().addMapLayer(lyr, False)
   QgsProject.instance().layerTreeRoot().insertLayer(1, lyr)  # 位置は重ねる順番に合わせる
   print(lyr.isValid(), lyr.extent().toString(1))
   ```

3. **検査線レイヤを作る。** 手作業の成果なので、保存先を利用者に確認する（例: `REPO/data/align/mapsuke/<名前>_checkpoints.gpkg`）。線（EPSG:6671）で、属性は `kind`（目印の種類、一覧から選ぶ）・`note`・`err_m`（`round($length, 3)` を既定値にし、更新時も計算）。`aligned` の頂点に吸着させ、編集を始めて線の追加ツールを選んでおく。

   ```python
   from qgis.core import (QgsVectorLayer, QgsProject, QgsFields, QgsField, QgsVectorFileWriter, QgsWkbTypes,
       QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsDefaultValue, QgsEditorWidgetSetup,
       QgsSnappingConfig, QgsTolerance, Qgis, QgsLineSymbol, QgsSingleSymbolRenderer)
   from qgis.PyQt.QtCore import QMetaType
   from qgis.utils import iface
   path = r"<確認した保存先>.gpkg"
   aligned_id = "<aligned レイヤの ID>"
   fields = QgsFields()
   fields.append(QgsField("kind", QMetaType.Type.QString))
   fields.append(QgsField("note", QMetaType.Type.QString))
   fields.append(QgsField("err_m", QMetaType.Type.Double))
   opts = QgsVectorFileWriter.SaveVectorOptions(); opts.driverName = "GPKG"; opts.layerName = "checkpoints"
   w = QgsVectorFileWriter.create(path, fields, Qgis.WkbType.LineString, QgsCoordinateReferenceSystem("EPSG:6671"),
                                  QgsCoordinateTransformContext(), opts)
   print(w.hasError(), w.errorMessage()); del w
   proj = QgsProject.instance()
   lyr = QgsVectorLayer(path + "|layername=checkpoints", "検査線", "ogr")
   lyr.setDefaultValueDefinition(lyr.fields().indexFromName("err_m"), QgsDefaultValue("round($length, 3)", True))
   lyr.setEditorWidgetSetup(lyr.fields().indexFromName("kind"), QgsEditorWidgetSetup("ValueMap", {"map": [
       {"道路端・宅地境": "道路端・宅地境"}, {"塀・擁壁の角": "塀・擁壁の角"}, {"街区の角": "街区の角"},
       {"水路・法面": "水路・法面"}, {"その他": "その他"}]}))
   lyr.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({"color": "#e31a1c", "width": "0.6"})))
   proj.addMapLayer(lyr, False); proj.layerTreeRoot().insertLayer(0, lyr)
   cfg = proj.snappingConfig(); cfg.setEnabled(True)
   cfg.setMode(QgsSnappingConfig.SnappingMode.AdvancedConfiguration)
   units = Qgis.MapToolUnit.Pixels if hasattr(Qgis, "MapToolUnit") else QgsTolerance.UnitType.Pixels
   cfg.setIndividualLayerSettings(proj.mapLayer(aligned_id),
       QgsSnappingConfig.IndividualLayerSettings(True, Qgis.SnappingType.Vertex, 12, units, 0.0, 0.0))
   proj.setSnappingConfig(cfg)
   iface.setActiveLayer(lyr); lyr.startEditing(); iface.actionAddFeature().trigger()
   ```

4. **線の引き方を案内する。** 線を引くのは利用者の手作業で、エージェントは代わりに引けない。
   - 1:300〜1:500 に拡大し、**筆界の角（始点、吸着する）** から **オルソ画像の対応する位置（終点）** へ線を引き、右クリックで確定。フォームで `kind` を選ぶ。
   - 目印は道路端と宅地の境目、塀・擁壁の角、街区の角など **地面の高さのもの**。屋根は倒れ込んで写るので使わない。
   - 終点を置くときは吸着を一時的に切る（S キー）と筆界の頂点に引き寄せられない。
   - ずれのない点も短い線で取る（0m もデータ）。筆界は塀や道路端と必ず一致するわけではないので、対応がはっきりした点だけ選ぶ。
   - 目安は信頼できるエリアと要確認のエリアの両方から合わせて 15〜20 本（試しなら 3〜5 本）。終わったら検査線レイヤを Ctrl+S で保存してもらう。

5. **集計する（未検証）。** ⚠ この手順はまだ一度も実際の検査線で動かしていない。初めて使うときは結果を手で数本確かめ、問題があればこの SKILL.md を直すこと。
   始点を含む `aligned` の筆（なければ 0.5m 以内で最も近い筆）からエリアを決め、エリアごとに本数・平均・RMSE・最大を出し、`second_gap`・`reliable` と並べる。編集中なら先に保存されているか（`isModified()`）を確かめる。

   ```python
   import math
   from collections import defaultdict
   from qgis.core import QgsProject, QgsGeometry, QgsPointXY, QgsSpatialIndex
   proj = QgsProject.instance()
   cp = proj.mapLayer("<検査線レイヤの ID>")
   al = proj.mapLayer("<aligned レイヤの ID>")
   print("未保存の編集:", cp.isModified())
   feats = {f.id(): f for f in al.getFeatures()}
   idx = QgsSpatialIndex(al.getFeatures())
   by_area, info = defaultdict(list), {}
   for f in cp.getFeatures():
       g = f.geometry()
       if g.isEmpty():
           continue
       line = g.asPolyline() if not g.isMultipart() else g.asMultiPolyline()[0]
       start, length = QgsGeometry.fromPointXY(QgsPointXY(line[0])), g.length()
       area = None
       for fid in idx.nearestNeighbor(QgsPointXY(line[0]), 5):
           a = feats[fid]
           if a.geometry().intersects(start.buffer(0.5, 4)):
               area = a["area"]
               info[area] = (a["reliable"], a["second_gap"], a["theta"], a["scale"])
               break
       by_area[area].append(length)
   for area, ls in sorted(by_area.items(), key=lambda x: str(x[0])):
       rmse = math.sqrt(sum(v * v for v in ls) / len(ls))
       print(area, len(ls), f"平均 {sum(ls)/len(ls):.2f} m  RMSE {rmse:.2f} m  最大 {max(ls):.2f} m", info.get(area))
   allv = [v for ls in by_area.values() for v in ls]
   if allv:
       print("全体", len(allv), f"RMSE {math.sqrt(sum(v*v for v in allv)/len(allv)):.2f} m")
   ```

   `None` のエリアに入った線は始点が筆界の上にない。吸着せずに引いた可能性があるので利用者に伝える。

6. **プロジェクトを保存する。** 未保存のプロジェクトは保存先を利用者に確認し（例: `REPO/data/align/mapsuke/<名前>_review.qgz`）、`QgsProject.instance().setFileName(path)` のあと `iface.actionSaveProject().trigger()` で保存する。保存済みなら `iface.actionSaveProject().trigger()` だけ。保存後に `isDirty()` が False になったことを確かめる。

## 報告するとき

- エリアごとの RMSE・最大と、`second_gap`・`reliable` の関係を伝える（gap が小さいエリアのずれが大きいか）。
- 検査線は人が選んだ目印との距離で、筆界と目印が一致しない分も含む。正解の座標との誤差ではない。
- `moj-georef` の文書にある精度（成功時の誤差の中央値 0.58m）は、**公共座標系**の図郭をわざと崩して戻した評価のもの。任意座標系での精度の根拠は、それまでオルソ画像との目視（1m 前後）だけだった。検査線の集計はその数値の裏付けになる。
- 位置合わせは相似変換だけなので、図面の端ほどずれが大きくなりやすい。点の場所の偏りにも触れる。
- 結果は実験段階のもので、登記や測量の判断に使わないよう伝える。
