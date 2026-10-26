import io

from moj_georef.catalog import parse_resource
from moj_georef.xml_summary import summarize

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<地図 xmlns="http://www.moj.go.jp/MINJI/tizuxml" xmlns:zmn="http://www.moj.go.jp/MINJI/tizuzumen">
  <version>ver1.0</version>
  <地図名>テスト</地図名>
  <市区町村コード>34101</市区町村コード>
  <市区町村名>広島市中区</市区町村名>
  <座標系>任意座標系</座標系>
  <空間属性>
    <zmn:GM_Point id="P1"><zmn:GM_Point.position><zmn:DirectPosition>
      <zmn:X>1.0</zmn:X><zmn:Y>2.0</zmn:Y>
    </zmn:DirectPosition></zmn:GM_Point.position></zmn:GM_Point>
  </空間属性>
  <主題属性>
    <筆 id="H1"><大字名>基町</大字名><地番>1</地番><座標値種別>図上測量</座標値種別></筆>
    <筆 id="H2"><大字名>基町</大字名><地番>2</地番><座標値種別>図上測量</座標値種別></筆>
    <筆 id="H3"><大字名>大手町</大字名><地番>3</地番><座標値種別>測量成果</座標値種別></筆>
  </主題属性>
  <図郭>
    <地図番号>1</地図番号>
    <縮尺分母>600</縮尺分母>
    <地図種類>旧土地台帳附属地図</地図種類>
    <地図分類>地図に準ずる図面</地図分類>
    <地図作成年月日><年>1889</年></地図作成年月日>
  </図郭>
</地図>
"""


def test_summarize_reads_header_and_counts():
    s = summarize(io.BytesIO(SAMPLE.encode("utf-8")), "sample.xml")
    assert s.city_code == "34101"
    assert s.is_arbitrary
    assert s.n_parcels == 3
    assert s.n_sheets == 1
    assert s.oaza == {"基町": 2, "大手町": 1}
    assert s.coord_kind["図上測量"] == 2
    assert s.map_class == {"地図に準ずる図面": 1}
    assert s.scale == {"600": 1}
    assert s.to_row()["year_min"] == 1889


def test_parse_resource():
    url = "https://example.com/download/34101-2400-2026.zip"
    ds = parse_resource("広島市中区", url, 100)
    assert ds is not None
    assert (ds.city_code, ds.year, ds.filename) == ("34101", 2026, "34101-2400-2026.zip")
    assert parse_resource("利用規約", "https://example.com/license.pdf", None) is None
