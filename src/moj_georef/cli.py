from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from moj_georef import catalog, export, parcels
from moj_georef.xml_summary import iter_city_zip


def cmd_survey(args: argparse.Namespace) -> None:
    """都道府県内の地図XMLを走査し、地区ごとの座標系などを CSV に書き出す。"""
    datasets = catalog.list_datasets(args.pref)
    if args.city:
        datasets = [d for d in datasets if d.city_code in args.city]
    if args.max_size_mb:
        datasets = [d for d in datasets if (d.size or 0) <= args.max_size_mb * 1e6]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter[str] | None = None
        for ds in datasets:
            print(f"{ds.city_code} {ds.title}", file=sys.stderr, flush=True)
            path = catalog.download(ds, args.raw_dir)
            for summary in iter_city_zip(path):
                row = summary.to_row()
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                rows_written += 1
            f.flush()
            if not args.keep:
                path.unlink()
    print(f"wrote {rows_written} rows to {args.out}", file=sys.stderr)


def cmd_prepare(args: argparse.Namespace) -> None:
    """市区町村 zip を筆ポリゴンの GPKG にする。

    公共座標系の地区はその座標系で、任意座標系の地区は局所座標のまま
    同じ EPSG を名目上付けて書き出す（QGIS で座標系原点付近に表示される）。
    """
    pub: list[export.Feature] = []
    arb: list[export.Feature] = []
    arb_blocks: list[export.Feature] = []
    sheets: dict[str, list[export.Feature]] = {"public": [], "arbitrary": []}
    epsgs: set[int] = set()

    for m in parcels.iter_city_zip(args.zip):
        base = {"xml": m.file, "map_name": m.header["地図名"], "crs": m.header["座標系"]}
        if m.epsg:
            epsgs.add(m.epsg)
        kind = "public" if m.epsg else "arbitrary"
        for s in m.sheets:
            sheets[kind].append(({**base, **s.attrs, "n_parcels": len(s.parcel_ids)}, s.geometry))
        if m.epsg:
            pub += [({**base, **p.attrs}, p.geometry) for p in m.parcels]
            continue
        block_of: dict[str, str] = {}
        for i, (geom, ids) in enumerate(parcels.blocks(m.parcels)):
            block_id = f"{Path(m.file).stem}#{i}"
            block_of.update(dict.fromkeys(ids, block_id))
            oaza = sorted({p.attrs.get("大字名", "") for p in m.parcels if p.id in set(ids)})
            props = {**base, "block": block_id, "n_parcels": len(ids), "oaza": ";".join(oaza)}
            arb_blocks.append((props, geom))
        arb += [
            ({**base, **p.attrs, "block": block_of.get(p.id, "")}, p.geometry) for p in m.parcels
        ]

    if len(epsgs) > 1:
        print(f"warning: 複数の公共座標系が混在: {sorted(epsgs)}", file=sys.stderr)
    epsg = min(epsgs) if epsgs else 6671
    out = args.out or Path("data/work") / f"{args.zip.stem}.gpkg"
    out.unlink(missing_ok=True)
    for layer, feats in [
        ("parcels_public", pub),
        ("parcels_arbitrary", arb),
        ("blocks_arbitrary", arb_blocks),
        ("sheets_public", sheets["public"]),
        ("sheets_arbitrary", sheets["arbitrary"]),
    ]:
        n = export.write_layer(out, layer, feats, epsg)
        print(f"{layer}: {n}", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)


def cmd_evaluate(args: argparse.Namespace) -> None:
    """公共座標系の図郭を崩して位置合わせし、元の位置との誤差を CSV に出す。"""
    import numpy as np
    import rasterio

    from moj_georef import evaluate

    public, cases = evaluate.load_city(args.zip)
    with rasterio.open(args.dem) as src:
        db = src.bounds
    margin = 400.0

    def inside(c: evaluate.Case) -> bool:
        x0, y0, x1, y1 = np.array([g.bounds for g in c.geoms]).T
        return (
            x0.min() - margin > db.left and x1.max() + margin < db.right
            and y0.min() - margin > db.bottom and y1.max() + margin < db.top
        )  # fmt: skip

    pool = [c for c in cases if len(c.geoms) >= args.min_parcels and inside(c)]
    rng = np.random.default_rng(args.seed)
    picked = [pool[i] for i in rng.choice(len(pool), size=min(args.n, len(pool)), replace=False)]
    print(f"{len(pool)} candidate sheets, evaluating {len(picked)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    gpkg = args.out.with_suffix(".gpkg")
    gpkg.unlink(missing_ok=True)
    truth_feats: list[export.Feature] = []
    found_feats: list[export.Feature] = []
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["case", "n_parcels", "true_theta", "true_scale", "found_theta", "found_scale",
             "score", "overlap", "second_gap", "error_mean", "error_max", "seconds"]
        )  # fmt: skip
        for c in picked:
            r = evaluate.run_case(c, public, args.dem, rng)
            w.writerow(
                [r.case, r.n_parcels, f"{r.truth.theta_deg:.2f}", f"{r.truth.scale:.4f}",
                 f"{r.found.theta_deg:.2f}", f"{r.found.scale:.4f}", f"{r.score:.2f}",
                 f"{r.overlap:.3f}", f"{r.second_gap:.2f}", f"{r.error_mean:.2f}",
                 f"{r.error_max:.2f}", f"{r.seconds:.1f}"]
            )  # fmt: skip
            f.flush()
            ok = r.error_mean < args.success_m
            for g, fg, e in zip(c.geoms, r.found_geoms, r.errors, strict=True):
                props = {"case": r.case, "success": ok, "case_error": r.error_mean,
                         "score": r.score, "second_gap": r.second_gap}  # fmt: skip
                truth_feats.append((props, g))
                found_feats.append(({**props, "error": e}, fg))
            if r.edges is not None:
                r.edges.write(gpkg.parent / "edges" / f"{len(truth_feats):06d}.tif")
            print(
                f"{r.case:32s} n={r.n_parcels:4d} score={r.score:.2f} gap={r.second_gap:.2f} "
                f"err={r.error_mean:.2f}m (max {r.error_max:.2f})",
                file=sys.stderr,
            )
    export.write_layer(gpkg, "truth", truth_feats, 6671)
    export.write_layer(gpkg, "found", found_feats, 6671)
    print(f"wrote {args.out} and {gpkg}", file=sys.stderr)


def cmd_align(args: argparse.Namespace) -> None:
    """任意座標系の地区を位置合わせし、結果を GPKG に出す。"""
    from shapely.geometry import box

    from moj_georef import align

    city = align.load_city(args.zip)
    targets = [m for m in city.arbitrary if not args.xml or m.file in args.xml]
    targets = [m for m in targets if len(m.parcels) >= args.min_parcels]
    print(f"{len(targets)} arbitrary XMLs", file=sys.stderr)

    out = args.out or Path("data/align") / f"{args.zip.stem}.gpkg"
    out.unlink(missing_ok=True)
    parcel_feats: list[export.Feature] = []
    summary_feats: list[export.Feature] = []
    for m in targets:
        bounds = align.search_bounds(m, city, args.margin)
        if bounds is None:
            print(f"{m.file}: 大字が公共座標系の地区に見つからないので飛ばす", file=sys.stderr)
            continue
        r = align.align_xml(m, city, args.dem, bounds)
        t = r.best.transform
        summary = {
            "xml": r.xml, "map_name": r.map_name, "n_parcels": len(r.geoms),
            "score": r.best.score, "penalty": r.best.overlap, "second_gap": r.second_gap,
            "theta": t.theta_deg, "scale": t.scale, "seconds": r.seconds,
        }  # fmt: skip
        summary_feats.append((summary, box(*r.bounds)))
        parcel_feats += [({**summary, **a}, g) for a, g in zip(r.attrs, r.geoms, strict=True)]
        print(
            f"{r.xml:20s} {r.map_name:16s} n={len(r.geoms):4d} score={r.best.score:.2f} "
            f"pen={r.best.overlap:.2f} gap={r.second_gap:.2f} theta={t.theta_deg:+.2f} "
            f"s={t.scale:.4f} ({r.seconds:.0f}s)",
            file=sys.stderr,
        )
    export.write_layer(out, "aligned", parcel_feats, 6671)
    export.write_layer(out, "search_window", summary_feats, 6671)
    print(f"wrote {out}", file=sys.stderr)


def cmd_geocode_align(args: argparse.Namespace) -> None:
    """地番ジオコードで粗く合わせ、--dem があれば DEM・筆界で細かく合わせる。"""
    import numpy as np
    from shapely import unary_union
    from shapely.geometry import LineString, Point

    from moj_georef import align, two_stage
    from moj_georef.geocode import GsiGeocoder

    city = align.load_city(args.zip)
    xmls = [m for m in city.arbitrary if not args.xml or m.file in args.xml]
    geocoder = GsiGeocoder(args.cache)
    try:
        areas = two_stage.build_areas(xmls, geocoder, args.pref_name, args.epsg, args.gap)
    finally:
        geocoder.save()

    # 対応点の多い（信頼できる）エリアから順に置き、置いた範囲は次のエリアの禁止領域にする
    areas.sort(key=lambda a: -a.n_unique)
    placed: list = []
    coarse_feats: list[export.Feature] = []
    final_feats: list[export.Feature] = []
    anchor_feats: list[export.Feature] = []
    residual_feats: list[export.Feature] = []
    for a in areas:
        if len(a.parcels) < args.min_parcels or a.coarse is None or a.coarse.transform is None:
            continue
        if args.dem and a.n_unique >= args.min_unique:
            two_stage.refine(a, city.public, args.dem, placed)
        c, f = a.coarse.transform, a.final
        summary = {
            "xml": a.xml.file, "map_name": a.xml.header["地図名"], "area": a.id,
            "n_parcels": len(a.parcels), "n_anchors": len(a.keys), "n_unique": a.n_unique,
            "coarse_rmse": a.coarse.rmse, "coarse_theta": c.theta_deg, "status": a.status,
            "score": f.score if f else None, "penalty": f.overlap if f else None,
            "second_gap": a.second_gap,
            "theta": f.transform.theta_deg if f else None,
            "scale": f.transform.scale if f else None,
        }  # fmt: skip
        print(
            f"{a.id:22s} {a.xml.header['地図名']:14s} parcels={len(a.parcels):4d} "
            f"anchors={len(a.keys):3d} unique={a.n_unique:3d} rmse={a.coarse.rmse:6.1f} "
            f"theta0={c.theta_deg:+7.2f} -> {a.status}"
            + (
                f" score={f.score:.2f} pen={f.overlap:.2f} gap={a.second_gap:.2f} "
                f"theta={f.transform.theta_deg:+.2f} s={f.transform.scale:.3f}"
                if f
                else ""
            ),
            file=sys.stderr,
        )
        attrs = [p.attrs for p in a.parcels]
        coarse_feats += [({**summary, **at}, g) for at, g in zip(attrs, a.geoms(c), strict=True)]
        if f:
            geoms = a.geoms(f.transform)
            final_feats += [({**summary, **at}, g) for at, g in zip(attrs, geoms, strict=True)]
            placed.append(unary_union(geoms))
        moved = c.apply(a.src)
        res = np.hypot(*(moved - a.dst).T)
        for k, p0, p1, r in zip(a.keys, moved, a.dst, res, strict=True):
            props = {"area": a.id, "honban": k, "residual": r}
            anchor_feats.append((props, Point(p1)))
            residual_feats.append((props, LineString([p0, p1])))

    out = args.out or Path("data/align") / f"{args.zip.stem}_geocode.gpkg"
    out.unlink(missing_ok=True)
    export.write_layer(out, "aligned", final_feats, args.epsg)
    export.write_layer(out, "aligned_coarse", coarse_feats, args.epsg)
    export.write_layer(out, "anchors", anchor_feats, args.epsg)
    export.write_layer(out, "residuals", residual_feats, args.epsg)
    print(f"wrote {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="moj-georef")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("survey", help="地区ごとの座標系・地図分類を集計する")
    p.add_argument("--pref", default="34", help="都道府県コード（既定: 34 広島県）")
    p.add_argument("--city", nargs="*", help="対象の市区町村コード")
    p.add_argument("--max-size-mb", type=float, help="この容量以下の zip だけ対象にする")
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    p.add_argument("--out", type=Path, default=Path("data/survey/34.csv"))
    p.add_argument("--keep", action="store_true", help="ダウンロードした zip を残す")
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("prepare", help="市区町村 zip を筆ポリゴンの GPKG にする")
    p.add_argument("zip", type=Path, help="市区町村単位の zip（例: data/raw/34101-2400-2026.zip）")
    p.add_argument("--out", type=Path, help="出力 GPKG（既定: data/work/<zip名>.gpkg）")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("evaluate", help="公共座標系の図郭を崩して位置合わせの精度を測る")
    p.add_argument("zip", type=Path, help="市区町村単位の zip")
    p.add_argument("--dem", required=True, help="DEM（GeoTIFF/VRT、EPSG は地図XMLと同じ系）")
    p.add_argument("--n", type=int, default=10, help="評価する図郭の数")
    p.add_argument("--min-parcels", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path("data/eval/result.csv"))
    p.add_argument("--success-m", type=float, default=5.0, help="成功とみなす平均誤差 [m]")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("align", help="任意座標系の地区を位置合わせする")
    p.add_argument("zip", type=Path, help="市区町村単位の zip")
    p.add_argument("--dem", required=True)
    p.add_argument("--xml", nargs="*", help="対象の XML ファイル名（既定: すべての任意座標系）")
    p.add_argument("--min-parcels", type=int, default=20)
    p.add_argument("--margin", type=float, default=500.0, help="大字の範囲から広げる距離 [m]")
    p.add_argument("--out", type=Path, help="出力 GPKG（既定: data/align/<zip名>.gpkg）")
    p.set_defaults(func=cmd_align)

    p = sub.add_parser("geocode-align", help="地番のジオコード結果で任意座標系を合わせる")
    p.add_argument("zip", type=Path, help="市区町村単位の zip")
    p.add_argument("--pref-name", default="広島県", help="住所の先頭に付ける都道府県名")
    p.add_argument("--epsg", type=int, default=6671, help="出力の平面直角座標系")
    p.add_argument("--xml", nargs="*", help="対象の XML ファイル名（既定: すべての任意座標系）")
    p.add_argument("--gap", type=float, default=15.0, help="エリア分割の距離 [m]")
    p.add_argument("--cache", type=Path, default=Path("data/cache/gsi_geocode.json"))
    p.add_argument("--dem", help="指定すると DEM・筆界で細かく合わせる")
    p.add_argument("--min-parcels", type=int, default=5)
    p.add_argument("--min-unique", type=int, default=3, help="細かく合わせる最小の対応点数")
    p.add_argument("--out", type=Path, help="出力 GPKG")
    p.set_defaults(func=cmd_geocode_align)

    args = parser.parse_args(argv)
    args.func(args)
