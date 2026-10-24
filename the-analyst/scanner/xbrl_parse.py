"""EDINETのXBRL→CSV（type=5）から、業績推移・セグメント情報・記述情報を取り出す。

実データ（トヨタ自動車=IFRS、任天堂=日本基準、キヤノン=米国基準）で確認した仕様:
- CSVはUTF-16LE・タブ区切り。列: 要素ID/項目名/コンテキストID/相対年度/連結・個別/期間・時点/ユニットID/単位/値
- 会社固有の拡張要素は「項目名」が空のことがある → 要素IDのローカル名で判定し、項目名は補助に使う
- 主要な経営指標等: 要素名の末尾が SummaryOfBusinessResults（会社固有は KeyFinancialData のことも）
- IFRS移行企業は5期推移に旧基準(US GAAP等)の列が混在する → DEIの会計基準に合う要素を優先
- コンテキストID: 有報 CurrentYearDuration / Prior{n}YearDuration、
  半期報告書 InterimDuration / Prior1InterimDuration（旧様式は CurrentYTDDuration / Prior1YTDDuration）
- セグメント: CurrentYearDuration_{会社固有の前置き}{名前}Member（前置き例 jpcrp030000-asr_E02144-000）。
  標準のメンバー（OtherReportableSegmentsMember, ReconcilingItemsMember 等）は前置きなし
- DEI（会計基準・期末日など）は FilingDateInstant
"""
from __future__ import annotations

import io
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

COLS = ["element", "label", "context", "rel_year", "cons", "period", "unit_id", "unit", "value"]

CTX_RE = re.compile(r"^(?:(Current)|Prior(\d+))?(Year|Interim|YTD|Quarter)(Duration|Instant)(?:_(.+))?$")
SUMMARY_RE = re.compile(r"(?:SummaryOfBusinessResults|KeyFinancialData)$")
STD_TAG_RE = re.compile(r"(IFRS|USGAAP|JMIS)")
FILER_PREFIX_RE = re.compile(r"^jp[a-z]+\d*-[a-z]+_E\d{5}-\d{3}")
MEMBER_RE = re.compile(r"^[A-Za-z0-9]+Member$")

# ---- 主要指標: 要素ローカル名の「コア」（会計基準タグと末尾を除いた部分）に完全一致。左ほど優先
ELEMENT_RULES: dict[str, list[str]] = {
    "revenue": [r"NetSales", r"Revenues?", r"OperatingRevenues?\d*", r"SalesRevenues?", r"NetSalesAndOperatingRevenues?",
                r"TotalRevenues?", r"NetSalesOfCompletedConstructionContracts", r"OperatingRevenueAndOperatingIncome"],
    "operating_income": [r"OperatingIncomeLoss", r"OperatingIncome", r"OperatingProfitLoss", r"OperatingProfit"],
    "ordinary_income": [r"OrdinaryIncomeLoss", r"OrdinaryProfitLoss"],
    "pretax_income": [r"ProfitLossBeforeTax", r"IncomeLossBeforeIncomeTaxes", r"IncomeBeforeIncomeTaxes"],
    "net_income": [r"ProfitLossAttributableToOwnersOfParent", r"NetIncomeLossAttributableToOwnersOfParent",
                   r"NetIncomeLoss", r"ProfitLoss"],
    "net_assets": [r"NetAssets", r"EquityAttributableToOwnersOfParent",
                   r"EquityIncludingPortionAttributableToNonControllingInterest", r"TotalEquity"],
    "total_assets": [r"TotalAssets"],
    "roe": [r"RateOfReturnOnEquity"],
    "operating_cf": [r"CashFlowsFromUsedInOperatingActivities"],
}
# ---- 項目名による補助判定（銀行の「経常収益」など、要素名で拾えない場合）
LABEL_RULES: dict[str, list[str]] = {
    "revenue": [r"売上高", r"売上収益", r"営業収益", r"経常収益", r"完成工事高", r"営業収入", r"収益合計"],
    "operating_income": [r"営業利益", r"事業利益"],
    "ordinary_income": [r"経常利益"],
    "pretax_income": [r"税引前", r"税金等調整前"],
    "net_income": [r"親会社株主に帰属する当期純", r"当期利益.*親会社の所有者", r"親会社の所有者に帰属する当期", r"当社株主に帰属する"],
    "net_assets": [r"純資産額", r"親会社の所有者に帰属する持分$", r"資本合計"],
    "total_assets": [r"総資産額", r"資産合計"],
    "roe": [r"自己資本利益率", r"親会社所有者帰属持分当期利益率", r"株主資本利益率"],
    "operating_cf": [r"営業活動によるキャッシュ・フロー"],
}
LABEL_EXCLUDE = re.compile(r"(原価|1株|一株|潜在株式|希薄化|平均臨時|比率|包括)")
INSTANT_METRICS = {"net_assets", "total_assets"}
BODY_METRICS = ("revenue", "operating_income", "ordinary_income", "net_income")

# ---- セグメント
SEG_EXT_RULES = [r"^RevenuesFromExternalCustomers", r"^SalesToExternalCustomers", r"^RevenueFromExternalCustomers",
                 r"ExternalCustomers", r"RevenueToCustomers"]
SEG_TOTAL_RULES = [r"^NetSales(IFRS)?$", r"^Revenues?(IFRS)?$", r"^SalesRevenues?(IFRS)?$", r"^OperatingRevenues?\d*(IFRS)?$",
                   r"^SalesAndFinancialServicesRevenue(IFRS)?$"]
SEG_PROFIT_RULES = [r"^SegmentProfitLoss", r"SegmentProfit", r"SegmentIncome", r"^OperatingIncome(Loss)?$",
                    r"^OperatingProfitLoss(IFRS)?$", r"^OperatingProfit", r"^OrdinaryIncome(Loss)?$"]
SEG_EXT_LABELS = [r"外部顧客"]
SEG_PROFIT_LABELS = [r"セグメント利益", r"セグメント損益", r"^営業利益", r"^事業利益"]
SEG_NOT_SEGMENT = {"TotalOfReportableSegmentsAndOthersMember", "ReportableSegmentsMember", "ReconcilingItemsMember",
                   "NonConsolidatedMember", "ConsolidatedMember"}
SEG_NOT_SEGMENT_RE = re.compile(r"(Elimination|Reconcil|Adjustment|Unallocated|CorporateExpense|CorporateAsset|"
                                r"Intersegment|^Total|Consolidated)", re.I)
OTHER_MEMBERS = {
    "OtherReportableSegmentsMember": "その他",
    "OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember": "その他",
}

# ---- 記述情報（テキストブロック）
TEXT_KEYS = {
    "business": ("DescriptionOfBusinessTextBlock", "事業の内容"),
    "policy": ("BusinessPolicyBusinessEnvironmentIssuesToAddress", "経営方針、経営環境及び対処すべき課題等"),
    "mdna": ("ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlows", "経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析"),
    "risks": ("BusinessRisksTextBlock", "事業等のリスク"),
    "segments_note": ("NotesSegmentInformationEtcConsolidatedFinancialStatements", "セグメント情報等（注記）"),
}


# ---------------------------------------------------------------- 読み込み
def read_csv_zip(blob: bytes) -> pd.DataFrame:
    frames = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            base = n.rsplit("/", 1)[-1]
            if not base.endswith(".csv") or not base.startswith("jpcrp"):
                continue  # 監査報告書(jpaud)などは除外
            raw = z.read(n)
            df = None
            for enc in ("utf-16", "utf-8-sig", "cp932"):
                try:
                    df = pd.read_csv(io.BytesIO(raw), sep="\t", encoding=enc, dtype=str, keep_default_na=False)
                    break
                except Exception:
                    df = None
            if df is not None and df.shape[1] >= 9:
                df = df.iloc[:, :9]
                df.columns = COLS
                frames.append(df)
    if not frames:
        raise ValueError("ZIP内に解析可能なCSV(jpcrp*.csv)がありません")
    df = pd.concat(frames, ignore_index=True).fillna("")
    parsed = df["context"].map(_parse_ctx)
    df["offset"] = parsed.map(lambda p: p[0] if p else -1).astype(int)
    df["kind"] = parsed.map(lambda p: p[1] if p else "")
    df["ptype"] = parsed.map(lambda p: p[2] if p else "")
    df["dims"] = parsed.map(lambda p: p[3] if p else "")
    df["local"] = df["element"].map(lambda e: e.split(":")[-1])
    df["label_n"] = df["label"].map(lambda s: unicodedata.normalize("NFKC", s).strip())
    df["num"] = df["value"].map(to_num)
    return df


def _parse_ctx(c: str):
    m = CTX_RE.match(c or "")
    if not m:
        return None
    off = int(m.group(2)) if m.group(2) else 0
    return off, m.group(3), m.group(4), m.group(5) or ""


def to_num(v) -> float | None:
    s = str(v).strip().replace(",", "")
    if s in ("", "－", "-", "―", "—", "NaN", "nan"):
        return None
    s = s.replace("△", "-").replace("▲", "-")
    try:
        return float(s)
    except ValueError:
        return None


def read_dei(df: pd.DataFrame) -> dict:
    d = {}
    for _, r in df[df["element"].str.startswith("jpdei_cor:")].iterrows():
        d.setdefault(r["local"], r["value"].strip())
    return {
        "standard": d.get("AccountingStandardsDEI", ""),  # "Japan GAAP" / "IFRS" / "US GAAP" / "JMIS"
        "consolidated": d.get("WhetherConsolidatedFinancialStatementsArePreparedDEI", "true") == "true",
        "period_type": d.get("TypeOfCurrentPeriodDEI", ""),  # FY / HY / Q2
        "period_end": d.get("CurrentPeriodEndDateDEI", ""),
        "fiscal_year_end": d.get("CurrentFiscalYearEndDateDEI", ""),
        "edinet_code": d.get("EDINETCodeDEI", ""),
    }


STD_LABEL = {"Japan GAAP": "日本基準", "IFRS": "IFRS", "US GAAP": "米国基準", "JMIS": "修正国際基準"}


def _std_fit(local: str, std: str) -> int:
    """要素名が会計基準に合うか: 2=基準タグ一致, 1=タグなし(中立), 0=他基準。"""
    tags = set(STD_TAG_RE.findall(local))
    want = {"IFRS": "IFRS", "US GAAP": "USGAAP", "JMIS": "JMIS"}.get(std)
    if not tags:
        return 1
    return 2 if want in tags else 0


def _label_std_fit(label: str, std: str) -> int:
    tags = {"IFRS"} if "(IFRS)" in label else ({"USGAAP"} if "(US GAAP)" in label else set())
    want = {"IFRS": "IFRS", "US GAAP": "USGAAP"}.get(std)
    if not tags:
        return 1
    return 2 if want in tags else 0


def _core(local: str) -> str:
    return STD_TAG_RE.sub("", SUMMARY_RE.sub("", local))


# ---------------------------------------------------------------- 業績推移
def extract_financials(df: pd.DataFrame, annual: bool = True) -> dict:
    """主要な経営指標等（有報=最大5期、半期=当中間期・前中間期）と損益計算書から主要指標を返す。"""
    dei = read_dei(df)
    std = dei["standard"]
    base = df[df["offset"] >= 0]
    if annual:
        base = base[base["kind"] == "Year"]
    else:  # 新様式 Interim、旧様式 YTD（3か月の Quarter は使わない）
        kind = "Interim" if (base["kind"] == "Interim").any() else "YTD"
        base = base[base["kind"] == kind]

    summary = base[base["local"].str.contains(SUMMARY_RE)]
    has_cons = not summary[summary["dims"] == ""].empty
    has_ind = not summary[summary["dims"] == "NonConsolidatedMember"].empty
    # 連結財務諸表を作る会社は連結、作らない会社は個別。ただし実際に値がある側を優先
    consolidated = (dei["consolidated"] and has_cons) or (has_cons and not has_ind) or (not has_cons and not has_ind)
    dim_ok = "" if consolidated else "NonConsolidatedMember"
    summary = summary[summary["dims"] == dim_ok]
    body = base[(base["dims"] == dim_ok) & ~base["local"].str.contains(SUMMARY_RE)
                & ~base["local"].str.endswith("TextBlock") & ~base["element"].str.startswith("jpdei")]

    series: dict[str, dict[int, float]] = {}
    sources: dict[str, str] = {}
    for key in ELEMENT_RULES:
        ptype = "Instant" if key in INSTANT_METRICS else None
        cand = summary if ptype is None else summary[summary["ptype"] == ptype]
        got = _pick_by_element(cand, ELEMENT_RULES[key], std) or _pick_by_label(cand, LABEL_RULES[key], std)
        if not got and key in BODY_METRICS:
            bc = body[body["ptype"] == "Duration"]
            got = _pick_by_element(bc, ELEMENT_RULES[key], std)
        if got:
            series[key], sources[key] = got

    emp = base[(base["local"] == "NumberOfEmployees") & (base["ptype"] == "Instant") & (base["dims"] == dim_ok)]
    if not emp.empty:
        vals = {int(r["offset"]): r["num"] for _, r in emp.iterrows() if r["num"] is not None}
        if vals:
            series["employees"], sources["employees"] = vals, "jpcrp_cor:NumberOfEmployees"

    return {"basis": "連結" if consolidated else "個別", "standard": STD_LABEL.get(std, std or "不明"),
            "standard_raw": std, "series": series, "sources": sources, "dei": dei}


def _values(g: pd.DataFrame) -> dict[int, float]:
    vals: dict[int, float] = {}
    for _, r in g.iterrows():
        if r["num"] is not None and int(r["offset"]) not in vals:
            vals[int(r["offset"])] = r["num"]
    return vals


def _pick_by_element(cand: pd.DataFrame, rules: list[str], std: str):
    if cand.empty:
        return None
    cores = cand["local"].map(_core)
    for pat in rules:
        rx = re.compile("^" + pat + "$")
        m = cand[cores.map(lambda c: bool(rx.match(c)))]
        best = None
        for el, g in m.groupby("element", sort=False):
            fit = _std_fit(g.iloc[0]["local"], std)
            if fit == 0:
                continue
            vals = _values(g)
            if not vals:
                continue
            score = (fit, 0 in vals, len(vals))
            if best is None or score > best[0]:
                best = (score, vals, el)
        if best:
            return best[1], best[2]
    return None


def _pick_by_label(cand: pd.DataFrame, rules: list[str], std: str):
    if cand.empty:
        return None
    for pat in rules:
        rx = re.compile("^" + pat)
        m = cand[cand["label_n"].map(lambda s: bool(rx.search(s)) and not LABEL_EXCLUDE.search(s))]
        best = None
        for el, g in m.groupby("element", sort=False):
            fit = _label_std_fit(g.iloc[0]["label_n"], std)
            if fit == 0:
                continue
            vals = _values(g)
            if vals and (best is None or (fit, len(vals)) > best[0]):
                best = ((fit, len(vals)), vals, el)
        if best:
            return best[1], best[2]
    return None


# ---------------------------------------------------------------- セグメント
def segment_member(dims: str) -> str | None:
    """'jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember' → 'AutomotiveReportableSegmentMember'。
    複数軸のコンテキストや単体のものは None。"""
    if not dims or "NonConsolidated" in dims:
        return None
    m = FILER_PREFIX_RE.match(dims)
    member = dims[m.end():] if m else dims
    return member if MEMBER_RE.match(member) else None


def _is_segment(member: str) -> bool:
    if member in SEG_NOT_SEGMENT:
        return False
    if member in OTHER_MEMBERS or "ReportableSegment" in member:
        return True
    return not SEG_NOT_SEGMENT_RE.search(member)


def extract_segments(df: pd.DataFrame, member_labels: dict[str, str] | None = None, annual: bool = True) -> list[dict]:
    member_labels = member_labels or {}
    std = read_dei(df)["standard"]
    kind = "Year" if annual else ("Interim" if (df["kind"] == "Interim").any() else "YTD")
    seg = df[(df["kind"] == kind) & (df["ptype"] == "Duration") & df["offset"].isin([0, 1]) & (df["dims"] != "")].copy()
    if seg.empty:
        return []
    seg["member"] = seg["dims"].map(segment_member)
    seg = seg[seg["member"].notna()]
    seg = seg[seg["member"].map(_is_segment)]
    if seg.empty:
        return []

    def collect(rules, labels, allowed=None):
        for pat in rules:
            rx = re.compile(pat)
            m = seg[seg["local"].map(lambda l: bool(rx.search(l)) and "Intersegment" not in l)]
            got = _members_of(m, std, allowed)
            if got:
                return got
        for pat in labels:
            rx = re.compile(pat)
            m = seg[seg["label_n"].map(lambda s: bool(rx.search(s)))]
            got = _members_of(m, std, allowed)
            if got:
                return got
        return {}

    rev = collect(SEG_EXT_RULES, SEG_EXT_LABELS)
    rev_basis = "外部顧客への売上"
    if not rev:
        rev = collect(SEG_TOTAL_RULES, [])
        rev_basis = "セグメント売上（内部取引含む）"
    if not rev:
        return []
    names = set(rev)
    if any("Segment" in n for n in names):  # 地域別・製品別などの別軸が混ざらないよう、セグメント名のメンバーに限定
        names = {n for n in names if "Segment" in n or n in OTHER_MEMBERS}
    prof = collect(SEG_PROFIT_RULES, SEG_PROFIT_LABELS, allowed=names)

    out = []
    for member in sorted(names, key=lambda n: -(rev.get(n, {}).get(0) or 0)):
        r, p = rev.get(member, {}), prof.get(member, {})
        if r.get(0) is None and p.get(0) is None:
            continue
        out.append({
            "segment": member_labels.get(member) or OTHER_MEMBERS.get(member) or _humanize(member),
            "member": member, "revenue_basis": rev_basis,
            "revenue": r.get(0), "revenue_prior": r.get(1),
            "profit": p.get(0), "profit_prior": p.get(1),
        })
    return out


def _members_of(m: pd.DataFrame, std: str, allowed=None) -> dict[str, dict[int, float]]:
    """要素ごとに {member: {offset: value}} を作り、当期値のあるメンバー数が最多の要素を返す。"""
    best, best_cov = {}, 0
    for el, g in m.groupby("element", sort=False):
        if _std_fit(g.iloc[0]["local"], std) == 0:
            continue
        vals: dict[str, dict[int, float]] = {}
        for _, r in g.iterrows():
            if r["num"] is None or (allowed is not None and r["member"] not in allowed):
                continue
            vals.setdefault(r["member"], {}).setdefault(int(r["offset"]), r["num"])
        cov = sum(0 in d for d in vals.values())
        if cov > best_cov:
            best, best_cov = vals, cov
    return best


def _humanize(member: str) -> str:
    s = re.sub(r"(Reportable)?(Business)?Segments?Member$|Member$", "", member)
    if s in ("Other", "Others", ""):
        return "その他"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)


def member_labels_from_xbrl_zip(blob: bytes) -> dict[str, str]:
    """提出本文書ZIP(type=1)の会社固有ラベル(*_lab.xml)から、メンバー名→日本語名 を得る。"""
    out: dict[str, str] = {}
    X = "{http://www.w3.org/1999/xlink}"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if "PublicDoc" not in n or not n.endswith("_lab.xml"):
                continue
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError:
                continue
            locs, labs, arcs = {}, {}, []
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "loc":
                    locs[el.get(X + "label")] = (el.get(X + "href") or "").split("#")[-1]
                elif tag == "label" and (el.get(X + "role") or "").endswith("/role/label"):
                    labs[el.get(X + "label")] = (el.text or "").strip()
                elif tag == "labelArc":
                    arcs.append((el.get(X + "from"), el.get(X + "to")))
            for f, t in arcs:
                if f in locs and labs.get(t):
                    href_id = locs[f]
                    m = re.match(r"^jp[a-z]+\d*-[a-z]+_E\d{5}-\d{3}_(.+)$", href_id)
                    local = m.group(1) if m else href_id.split("_", 1)[-1] if href_id.startswith("jp") else href_id
                    if local.endswith("Member"):
                        out.setdefault(local, labs[t])
    return out


# ---------------------------------------------------------------- 記述情報
def extract_texts(df: pd.DataFrame, max_chars: int = 8000) -> dict[str, str]:
    out = {}
    tb = df[df["local"].str.endswith("TextBlock")]
    for key, (needle, _title) in TEXT_KEYS.items():
        m = tb[tb["local"].str.contains(needle, regex=False)]
        if m.empty:
            continue
        txt = max(m["value"].tolist(), key=len)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"[ \t　]+", " ", txt)
        txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
        if txt:
            out[key] = txt[:max_chars]
    return out
