"""位置合わせ結果を確認する QGIS プロジェクトを作る。

QGIS 同梱の Python で実行する:

    "C:/Program Files/QGIS 4.0.0/bin/python-qgis.bat" qgis/make_review_project.py \
        --out data/qgis/kumano_review.qgz \
        --background data/qgis/kumano_csmap.tif \
        --parcels data/work/34307-2400-2026.gpkg \
        --eval data/eval/kumano_v4.gpkg --edges data/qgis/eval_edges.vrt \
        --aligned data/align/34307-2400-2026.gpkg

存在しないファイルを指定した引数は飛ばす。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsLayerTreeGroup,
    QgsPalettedRasterRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor


def fill(color: str, outline: str, width: float, style: str = "solid", line: str = "solid"):
    return QgsFillSymbol.createSimple(
        {
            "color": color,
            "style": style,
            "outline_color": outline,
            "outline_width": str(width),
            "outline_style": line,
        }
    )


def outline(color: str, width: float, line: str = "solid"):
    return fill("0,0,0,0", color, width, "no", line)


def flag_renderer(field: str, on: tuple, off: tuple) -> QgsRuleBasedRenderer:
    """真偽値の列で塗り分ける（GPKG の真偽値は QGIS では bool として読まれるので式で判定する）。"""
    rules = QgsRuleBasedRenderer(QgsRuleBasedRenderer.Rule(None))
    for (symbol, label), expr in ((on, f'"{field}"'), (off, "ELSE")):
        rules.rootRule().appendChild(QgsRuleBasedRenderer.Rule(symbol, 0, 0, expr, label))
    return rules


def vector(path: Path, layer: str, name: str) -> QgsVectorLayer | None:
    lyr = QgsVectorLayer(f"{path}|layername={layer}", name, "ogr")
    return lyr if lyr.isValid() else None


def add(group: QgsLayerTreeGroup, lyr, visible: bool = True) -> None:
    QgsProject.instance().addMapLayer(lyr, False)
    node = group.addLayer(lyr)
    node.setItemVisibilityChecked(visible)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--background", type=Path, help="背景ラスタ（CS立体図など）")
    ap.add_argument("--dem", type=Path, help="DEM（陰影起伏で表示、既定で非表示）")
    ap.add_argument("--parcels", type=Path, help="moj-georef prepare の出力 GPKG")
    ap.add_argument("--eval", type=Path, help="moj-georef evaluate の出力 GPKG")
    ap.add_argument("--edges", type=Path, help="評価に使ったエッジ（VRT）")
    ap.add_argument("--aligned", type=Path, help="moj-georef align の出力 GPKG")
    ap.add_argument("--geocode", type=Path, help="moj-georef geocode-align の出力 GPKG")
    ap.add_argument("--ortho", type=Path, help="オルソ画像（背景）")
    args = ap.parse_args()
    # 相対パスのままだと QGIS はプロジェクトファイルからの相対パスとして解釈するので、絶対パスにする
    for k in ("background", "dem", "parcels", "eval", "edges", "aligned", "geocode", "ortho"):
        if (v := getattr(args, k)) is not None:
            setattr(args, k, v.resolve())

    project = QgsProject.instance()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:6671"))
    root = project.layerTreeRoot()

    if args.geocode and args.geocode.exists():
        g = root.addGroup("地番ジオコードによる位置合わせ（正解なし）")
        if lyr := vector(args.geocode, "residuals", "残差（変換後の重心→ジオコード）"):
            from qgis.core import QgsLineSymbol

            lyr.setRenderer(
                QgsRuleBasedRenderer(
                    QgsLineSymbol.createSimple({"color": "#ff1744", "width": "0.4"})
                )
            )
            add(g, lyr)
        if lyr := vector(args.geocode, "anchors", "ジオコード点（番地）"):
            from qgis.core import QgsMarkerSymbol

            lyr.setRenderer(
                QgsRuleBasedRenderer(
                    QgsMarkerSymbol.createSimple(
                        {
                            "name": "circle",
                            "color": "#ffea00",
                            "size": "1.6",
                            "outline_color": "black",
                        }
                    )
                )
            )
            add(g, lyr)
        if lyr := vector(args.geocode, "aligned", "位置合わせ後の筆（ジオコード＋DEM＋基盤地図）"):
            lyr.setRenderer(
                flag_renderer(
                    "reliable",
                    (outline("#ffd400", 0.5), "信頼できる（gap≧1）"),
                    (outline("#ff1744", 0.5, "dash"), "要確認"),
                )
            )
            add(g, lyr)
        if lyr := vector(args.geocode, "aligned_coarse", "位置合わせ後の筆（ジオコードのみ）"):
            lyr.setRenderer(QgsRuleBasedRenderer(outline("#d500f9", 0.4, "dash")))
            add(g, lyr, visible=False)

    if args.aligned and args.aligned.exists():
        g = root.addGroup("任意座標系の位置合わせ結果（正解なし）")
        if lyr := vector(args.aligned, "aligned", "位置合わせ後の筆"):
            r = QgsCategorizedSymbolRenderer("map_name")
            palette = ["#ffd400", "#00e5ff", "#ff6d00", "#76ff03", "#d500f9", "#ff1744"]
            names = sorted({f["map_name"] for f in lyr.getFeatures()})
            for i, n in enumerate(names):
                r.addCategory(QgsRendererCategory(n, outline(palette[i % len(palette)], 0.5), n))
            lyr.setRenderer(r)
            add(g, lyr)
        if lyr := vector(args.aligned, "search_window", "探索範囲"):
            lyr.setRenderer(QgsRuleBasedRenderer(outline("255,255,255,200", 0.4, "dash")))
            add(g, lyr, visible=False)

    if args.eval and args.eval.exists():
        g = root.addGroup("評価（公共座標系の図郭を崩して戻す）")
        if lyr := vector(args.eval, "found", "結果の位置"):
            lyr.setRenderer(
                flag_renderer(
                    "success",
                    (outline("#ff00ff", 0.5), "成功（平均誤差5m未満）"),
                    (outline("#ff1744", 0.5, "dash"), "失敗"),
                )
            )
            add(g, lyr)
        if lyr := vector(args.eval, "truth", "正解の位置"):
            lyr.setRenderer(QgsRuleBasedRenderer(outline("#00e676", 0.6)))
            add(g, lyr)
        if args.edges and args.edges.exists():
            lyr = QgsRasterLayer(str(args.edges), "参照エッジ（DEM曲率＋公共筆界）")
            if lyr.isValid():
                classes = [QgsPalettedRasterRenderer.Class(1, QColor(255, 60, 0, 200), "エッジ")]
                lyr.setRenderer(QgsPalettedRasterRenderer(lyr.dataProvider(), 1, classes))
                add(g, lyr, visible=False)

    if args.parcels and args.parcels.exists():
        g = root.addGroup("地図XML")
        if lyr := vector(args.parcels, "parcels_public", "公共座標系の筆"):
            rules = QgsRuleBasedRenderer(QgsRuleBasedRenderer.Rule(None))
            base = rules.rootRule()
            pseudo = QgsRuleBasedRenderer.Rule(
                fill("255,145,0,60", "255,145,0,160", 0.2), 0, 0, "\"地番\" LIKE '地区外%'",
                "地区外（擬似筆）",
            )  # fmt: skip
            real = QgsRuleBasedRenderer.Rule(outline("0,229,255,150", 0.15), 0, 0, "ELSE", "筆")
            base.appendChild(pseudo)
            base.appendChild(real)
            lyr.setRenderer(rules)
            add(g, lyr)

    g = root.addGroup("背景")
    if args.dem and args.dem.exists():
        from qgis.core import QgsHillshadeRenderer

        lyr = QgsRasterLayer(str(args.dem), "DEM 陰影起伏")
        if lyr.isValid():
            lyr.setRenderer(QgsHillshadeRenderer(lyr.dataProvider(), 1, 315, 45))
            add(g, lyr, visible=False)
    if args.ortho and args.ortho.exists():
        lyr = QgsRasterLayer(str(args.ortho), "オルソ画像")
        if lyr.isValid():
            lyr.setCrs(QgsCoordinateReferenceSystem("EPSG:6671"))  # tfw のみで CRS 情報がない
            add(g, lyr)
    if args.background and args.background.exists():
        lyr = QgsRasterLayer(str(args.background), "CS立体図")
        if lyr.isValid():
            add(g, lyr, visible=not (args.ortho and args.ortho.exists()))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    project.write(str(args.out.resolve()))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        main()
    finally:
        qgs.exitQgis()
