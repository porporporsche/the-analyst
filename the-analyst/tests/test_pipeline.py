"""EDINET形式を模した合成データで、解析〜レポート生成までを検証する（ネットワーク不要）。"""
import io
import json
import zipfile
from pathlib import Path

from scanner import analyze
from scanner.analyze import company_metrics, segment_universe
from scanner.report import build_report
from scanner.xbrl_parse import extract_financials, extract_segments, extract_texts, member_labels_from_xbrl_zip, read_csv_zip

HDR = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]
E = "jpcrp030000-asr_E99999-000"


def _row(el, label, ctx, val, cons="連結"):
    return [el, label, ctx, "", cons, "期間", "JPY", "円", str(val)]


def make_csv_zip() -> bytes:
    rows = []
    for k, (rev, oi, ni) in enumerate([(1000, 120, 80), (900, 90, 60), (800, 70, 50), (700, 50, 30), (650, 40, 20)]):
        ctx = "CurrentYearDuration" if k == 0 else f"Prior{k}YearDuration"
        rows += [
            _row("jpcrp_cor:NetSalesSummaryOfBusinessResults", "売上高、経営指標等", ctx, rev * 1e8),
            _row("jpcrp_cor:OrdinaryIncomeLossSummaryOfBusinessResults", "経常利益又は経常損失（△）、経営指標等", ctx, (oi + 5) * 1e8),
            _row("jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
                 "親会社株主に帰属する当期純利益又は親会社株主に帰属する当期純損失（△）、経営指標等", ctx, ni * 1e8),
            _row("jpcrp_cor:NetSalesSummaryOfBusinessResults", "売上高、経営指標等", ctx + "_NonConsolidatedMember", rev * 0.4e8, "個別"),
            _row("jpcrp_cor:RateOfReturnOnEquitySummaryOfBusinessResults", "自己資本利益率、経営指標等", ctx, 0.1),
        ]
    # 損益計算書本体の営業利益（当期・前期）
    rows += [_row("jppfs_cor:OperatingIncome", "営業利益又は営業損失（△）", "CurrentYearDuration", 120e8),
             _row("jppfs_cor:OperatingIncome", "営業利益又は営業損失（△）", "Prior1YearDuration", 90e8),
             _row("jppfs_cor:OperatingIncomeToNetSalesRatio", "営業利益率", "CurrentYearDuration", 0.12)]
    # セグメント
    for mem, (r0, r1, p0, p1) in {"Cloud": (600, 480, 110, 80), "Hardware": (400, 420, -10, 5)}.items():
        for off, rv, pv in ((0, r0, p0), (1, r1, p1)):
            ctx = ("CurrentYearDuration" if off == 0 else "Prior1YearDuration") + f"_{E}{mem}ReportableSegmentMember"
            rows += [_row("jpcrp_cor:RevenuesFromExternalCustomers", "外部顧客への売上高", ctx, rv * 1e8),
                     _row("jpcrp_cor:TransactionsWithOtherSegments", "セグメント間の内部売上高又は振替高", ctx, 7e8),
                     _row("jpcrp_cor:SegmentProfitLoss", "セグメント利益又は損失（△）", ctx, pv * 1e8)]
    rows.append(_row("jpcrp_cor:DescriptionOfBusinessTextBlock", "事業の内容 [テキストブロック]", "FilingDateInstant",
                     "<p>当社はクラウド事業と機器販売事業を営んでいます。</p>"))
    buf = io.StringIO()
    buf.write("\t".join(HDR) + "\n")
    for r in rows:
        buf.write("\t".join(f'"{x}"' for x in r) + "\n")
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("XBRL_TO_CSV/jpcrp030000-asr-001_E99999-000_2026-03-31_01_2026-06-20.csv", buf.getvalue().encode("utf-16"))
        zf.writestr("XBRL_TO_CSV/jpaud-aar-cn-001_E99999-000.csv", "x".encode("utf-16"))
    return z.getvalue()


def make_xbrl_zip() -> bytes:
    lab = f"""<?xml version="1.0" encoding="UTF-8"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
<link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
<link:loc xlink:type="locator" xlink:href="x.xsd#{E}_CloudReportableSegmentMember" xlink:label="CloudReportableSegmentMember"/>
<link:label xlink:type="resource" xlink:label="label_Cloud" xlink:role="http://www.xbrl.org/2003/role/label" xml:lang="ja">クラウド事業</link:label>
<link:labelArc xlink:type="arc" xlink:from="CloudReportableSegmentMember" xlink:to="label_Cloud"/>
</link:labelLink></link:linkbase>"""
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E99999-000_2026-03-31_01_2026-06-20_lab.xml", lab)
    return z.getvalue()


def test_parse_financials_and_segments():
    df = read_csv_zip(make_csv_zip())
    fin = extract_financials(df)
    s = fin["series"]
    assert fin["basis"] == "連結"
    assert s["revenue"][0] == 1000e8 and s["revenue"][4] == 650e8   # 個別値を拾わない
    assert s["operating_income"] == {0: 120e8, 1: 90e8}             # 営業利益率を誤って拾わない
    assert s["roe"][0] == 0.1
    assert s["net_income"][2] == 50e8

    labels = member_labels_from_xbrl_zip(make_xbrl_zip())
    segs = {x["segment"]: x for x in extract_segments(df, labels)}
    assert set(segs) == {"クラウド事業", "Hardware"}
    assert segs["クラウド事業"]["revenue"] == 600e8 and segs["クラウド事業"]["profit_prior"] == 80e8
    assert segs["Hardware"]["profit"] == -10e8
    assert "クラウド事業" in extract_texts(df)["business"]


def test_metrics_and_report(tmp_path: Path):
    df = read_csv_zip(make_csv_zip())
    c = {"edinet_code": "E99999", "sec_code": "9999", "name": "テスト株式会社", "industry": "情報・通信業",
         "annual": {**extract_financials(df), "period_end": "2026-03-31", "doc_id": "S100TEST", "submit_date": "2026-06-20"},
         "segments": extract_segments(df, member_labels_from_xbrl_zip(make_xbrl_zip())), "texts": extract_texts(df), "errors": []}
    m = company_metrics(c)
    assert m["period"] == "2026/03期"
    assert abs(m["margin"] - 0.12) < 1e-9
    assert abs(m["yoy"] - (1000 / 900 - 1)) < 1e-9
    assert m["trend"][0]["period"] == "2022/03期" and m["trend"][-1]["period"] == "2026/03期"
    c["metrics"] = m
    c2 = json.loads(json.dumps(c))  # 2社目（コピー）で比較チャートも通す
    c2.update(name="比較株式会社", edinet_code="E99998")
    c2["metrics"] = company_metrics({**c, "name": "比較株式会社"})
    c["insight"] = {"summary": "要約", "profitable": [{"business": "クラウド", "evidence": "利益率18%"}], "struggling": [], "strategy": ["x"], "ma_view": "y"}
    result = {
        "title": "テスト", "slug": "t", "generated_at": "2026-10-15 12:00 JST", "mode": "company", "subject": "テスト",
        "params": {"news_days": 90}, "llm_enabled": True, "llm_model": "test", "companies": [c, c2],
        "news": {"items": [{"id": "n1", "title": "見出し", "link": "https://example.com", "source": "媒体", "date": "2026-10-01",
                            "snippet": "", "query": "q", "channel": "Webニュース"}], "queries": ["q"], "feeds": [], "errors": []},
        "news_digest": {"overview": "概況", "growing": [{"area": "A", "evidence": "B", "article_ids": ["n1"]}], "themes": [], "deals": []},
        "synthesis": {"headline": "結論", "profitable": [{"business": "SaaS", "why": "w", "examples": "e", "source": "IR"}],
                      "unprofitable": [], "ir_vs_news": ["g"], "ma_implications": ["m"]},
        "highlights": analyze.rule_highlights([c, c2], segment_universe([c, c2])), "warnings": [],
    }
    out = build_report(result, tmp_path / "reports")
    html = (out / "index.html").read_text("utf-8")
    assert "クラウド事業" in html and "結論" in html and "概況" in html
    assert (tmp_path / "index.html").exists()
    assert "_trend_figs" not in (out / "data.json").read_text("utf-8")


def test_parse_json_with_fence():
    assert analyze._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
