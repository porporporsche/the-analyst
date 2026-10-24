"""実際のEDINET書類（type=5 CSV）で解析結果を検証する。期待値は各社の有価証券報告書の公表値。"""
import io
import zipfile
from pathlib import Path

import pytest

from scanner.analyze import company_metrics
from scanner.edinet import parse_codelist
from scanner.xbrl_parse import extract_financials, extract_segments, extract_texts, read_csv_zip, segment_member

FX = Path(__file__).parent / "fixtures"
OKU = 1e8


def load(name):
    return read_csv_zip((FX / name).read_bytes())


@pytest.fixture(scope="module")
def toyota():
    return load("csv_toyota_ifrs_S100TR7I.zip")


@pytest.fixture(scope="module")
def nintendo():
    return load("csv_nintendo_jpgaap_S100TMMG.zip")


@pytest.fixture(scope="module")
def canon():
    return load("csv_canon_usgaap_S100T58N.zip")


def test_toyota_ifrs(toyota):
    fin = extract_financials(toyota)
    s = fin["series"]
    assert fin["standard"] == "IFRS" and fin["basis"] == "連結"
    # 会社固有要素（項目名が空）の営業収益を5期分取れ、旧US GAAP列を拾わない
    assert round(s["revenue"][0] / OKU) == 450953 and len(s["revenue"]) == 5
    assert round(s["revenue"][4] / OKU) == 298665
    assert round(s["operating_income"][0] / OKU) == 53529
    assert round(s["net_income"][0] / OKU) == 49449
    assert s["roe"][0] == pytest.approx(0.158, abs=1e-3)
    assert s["employees"][0] == 380793

    segs = {x["member"]: x for x in extract_segments(toyota)}
    # 調整額（CorporateExpensesAndEliminationMember）は除外、標準の「その他」は含む
    assert set(segs) == {"AutomotiveReportableSegmentMember", "FinancialServicesReportableSegmentMember",
                         "OtherReportableSegmentsMember"}
    auto = segs["AutomotiveReportableSegmentMember"]
    assert round(auto["revenue"] / OKU) == 410807 and round(auto["profit"] / OKU) == 46215
    assert round(auto["revenue_prior"] / OKU) == 337769
    assert segs["OtherReportableSegmentsMember"]["segment"] == "その他"
    assert auto["revenue_basis"] == "外部顧客への売上"


def test_nintendo_jgaap(nintendo):
    fin = extract_financials(nintendo)
    s = fin["series"]
    assert fin["standard"] == "日本基準"
    assert round(s["revenue"][0] / OKU) == 16719 and len(s["revenue"]) == 5
    assert round(s["operating_income"][0] / OKU) == 5289   # 損益計算書本体から
    assert round(s["ordinary_income"][0] / OKU) == 6805
    assert round(s["net_income"][0] / OKU) == 4906
    assert extract_segments(nintendo) == []  # 単一セグメント
    texts = extract_texts(nintendo)
    assert {"business", "mdna", "risks"} <= set(texts)


def test_canon_usgaap(canon):
    fin = extract_financials(canon)
    s = fin["series"]
    assert fin["standard"] == "米国基準"
    assert round(s["revenue"][0] / OKU) == 41810
    assert round(s["net_income"][0] / OKU) == 2645
    assert "operating_income" not in s  # キヤノンのXBRLには営業利益がない
    c = {"name": "キヤノン", "annual": {**fin, "period_end": "2023-12-31"}, "segments": []}
    m = company_metrics(c)
    assert m["profit_label"] == "税引前利益" and m["period"] == "2023/12期"
    assert m["margin"] == pytest.approx(3907.7 / 41809.7, abs=1e-3)


def _rewrite(df_zip: bytes, repl: dict[str, str], name: str) -> bytes:
    """実データのCSVのコンテキストIDを置き換えて、半期報告書の形を作る。"""
    with zipfile.ZipFile(io.BytesIO(df_zip)) as z:
        src = next(n for n in z.namelist() if n.split("/")[-1].startswith("jpcrp"))
        text = z.read(src).decode("utf-16")
    for a, b in repl.items():
        text = text.replace(a, b)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"XBRL_TO_CSV/{name}", text.encode("utf-16"))
    return out.getvalue()


def test_interim_new_format():
    blob = _rewrite((FX / "csv_nintendo_jpgaap_S100TMMG.zip").read_bytes(),
                    {'"CurrentYearDuration': '"InterimDuration', '"Prior1YearDuration': '"Prior1InterimDuration',
                     '"FY"': '"HY"'},
                    "jpcrp040300-ssr-001_E02367-000_2024-09-30_01_2024-11-08.csv")
    fin = extract_financials(read_csv_zip(blob), annual=False)
    s = fin["series"]
    assert round(s["revenue"][0] / OKU) == 16719 and round(s["revenue"][1] / OKU) == 16017
    assert round(s["operating_income"][0] / OKU) == 5289


def test_interim_old_q2_uses_ytd_not_quarter():
    blob = _rewrite((FX / "csv_nintendo_jpgaap_S100TMMG.zip").read_bytes(),
                    {'"CurrentYearDuration': '"CurrentYTDDuration', '"Prior1YearDuration': '"Prior1YTDDuration'},
                    "jpcrp040000-q2r-001_E02367-000_2023-09-30_01_2023-11-08.csv")
    df = read_csv_zip(blob)
    # 3か月（Quarter）の行が先に現れても、累計（YTD）を使う
    q = df[df["kind"] == "YTD"].copy()
    q["kind"], q["num"] = "Quarter", q["num"].map(lambda v: None if v is None else v / 3)
    import pandas as pd
    df2 = pd.concat([q, df], ignore_index=True)
    fin = extract_financials(df2, annual=False)
    assert round(fin["series"]["revenue"][0] / OKU) == 16719


@pytest.mark.parametrize("dims,expected", [
    ("jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember", "AutomotiveReportableSegmentMember"),
    ("jpcrp040300-ssr_E00776-000FunctionalMaterialsReportableSegmentsMember", "FunctionalMaterialsReportableSegmentsMember"),
    ("OtherReportableSegmentsMember", "OtherReportableSegmentsMember"),
    ("NonConsolidatedMember", None),
    ("jpcrp030000-asr_E02274-000ImagingMember_jpcrp030000-asr_E02274-000JapanMember", None),
])
def test_segment_member(dims, expected):
    assert segment_member(dims) == expected


def test_parse_codelist_variants():
    header = ["ＥＤＩＮＥＴコード", "提出者種別", "上場区分", "連結の有無", "資本金", "決算日", "提出者名", "提出者名（英字）",
              "提出者名（ヨミ）", "所在地", "提出者業種", "証券コード", "提出者法人番号"]
    rows = [["ダウンロード実行日", "2026年10月15日現在", "件数", "11,000件"], header,
            ["E02144", "内国法人・組合", "上場", "有", "635401", "3月31日", "トヨタ自動車株式会社", "TOYOTA MOTOR CORPORATION",
             "トヨタジドウシャ", "愛知県豊田市", "輸送用機器", "72030", "1180301018771"]]
    text = "\r\n".join(",".join(f'"{c}"' for c in r) for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("EdinetcodeDlInfo.csv", text.encode("cp932"))
    out = parse_codelist(buf.getvalue())
    assert out == [{"edinet_code": "E02144", "filer_type": "内国法人・組合", "listed": "上場", "consolidated": "有",
                    "capital": "635401", "fiscal_end": "3月31日", "name": "トヨタ自動車株式会社",
                    "name_en": "TOYOTA MOTOR CORPORATION", "name_kana": "トヨタジドウシャ", "address": "愛知県豊田市",
                    "industry": "輸送用機器", "sec_code": "72030"}]
