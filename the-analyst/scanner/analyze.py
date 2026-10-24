"""指標計算とLLM(Claude API)による要約。ANTHROPIC_API_KEY が無い場合はルールベースで代替する。"""
from __future__ import annotations

import json
import os
import re
import statistics

# ---------------------------------------------------------------- 指標
def period_label(period_end: str, offset: int, interim: bool = False) -> str:
    try:
        y, m = int(period_end[:4]), int(period_end[5:7])
    except (TypeError, ValueError):
        return f"-{offset}期"
    return f"{y - offset}/{m:02d}{'中間' if interim else ''}期"


def _ratio(a, b):
    return a / b if (a is not None and b not in (None, 0)) else None


def _growth(cur, prev):
    return cur / prev - 1 if (cur is not None and prev not in (None, 0) and prev > 0) else None


def company_metrics(c: dict) -> dict:
    a = c.get("annual") or {}
    s = a.get("series") or {}
    rev = s.get("revenue", {})
    offs = sorted({int(k) for k in rev} | {int(k) for k in s.get("operating_income", {})}, reverse=True)
    pe = a.get("period_end", "")
    profit_key = next((k for k in ("operating_income", "ordinary_income", "pretax_income") if s.get(k, {}).get(0) is not None), None)
    profit = s.get(profit_key, {}) if profit_key else {}
    trend = [{
        "period": period_label(pe, k), "offset": k,
        "revenue": rev.get(k), "profit": profit.get(k), "net_income": s.get("net_income", {}).get(k),
        "margin": _ratio(profit.get(k), rev.get(k)),
    } for k in offs]
    first = max((k for k in rev if rev.get(k)), default=None)
    cagr = None
    if first and rev.get(0) and rev.get(first) and rev[first] > 0 and rev[0] > 0:
        cagr = (rev[0] / rev[first]) ** (1 / first) - 1
    m = {
        "period": period_label(pe, 0) if pe else "",
        "profit_label": {"operating_income": "営業利益", "ordinary_income": "経常利益", "pretax_income": "税引前利益"}.get(profit_key, "利益"),
        "revenue": rev.get(0), "profit": profit.get(0), "net_income": s.get("net_income", {}).get(0),
        "margin": _ratio(profit.get(0), rev.get(0)),
        "margin_prev": _ratio(profit.get(1), rev.get(1)),
        "yoy": _growth(rev.get(0), rev.get(1)),
        "profit_yoy": _growth(profit.get(0), profit.get(1)),
        "cagr": cagr, "cagr_years": first,
        "roe": s.get("roe", {}).get(0), "employees": s.get("employees", {}).get(0),
        "trend": trend,
    }
    it = c.get("interim")
    if it:
        si = it.get("series") or {}
        r, pk = si.get("revenue", {}), next((k for k in ("operating_income", "ordinary_income") if si.get(k, {}).get(0) is not None), None)
        p = si.get(pk, {}) if pk else {}
        m["interim"] = {
            "period": period_label(it.get("period_end", ""), 0, interim=True),
            "revenue": r.get(0), "revenue_yoy": _growth(r.get(0), r.get(1)),
            "profit": p.get(0), "profit_yoy": _growth(p.get(0), p.get(1)), "margin": _ratio(p.get(0), r.get(0)),
        }
    segs = []
    total = sum(x["revenue"] for x in c.get("segments", []) if x.get("revenue")) or None
    for x in c.get("segments", []):
        segs.append({**x,
                     "margin": _ratio(x.get("profit"), x.get("revenue")),
                     "growth": _growth(x.get("revenue"), x.get("revenue_prior")),
                     "profit_growth": _growth(x.get("profit"), x.get("profit_prior")),
                     "share": _ratio(x.get("revenue"), total)})
    m["segments"] = segs
    return m


def segment_universe(companies: list[dict]) -> list[dict]:
    rows = []
    for c in companies:
        for s in c.get("metrics", {}).get("segments", []):
            if s.get("revenue"):
                rows.append({"company": c["name"], **s})
    return rows


def rule_highlights(companies: list[dict], segs: list[dict]) -> list[str]:
    """LLMを使わない場合の自動所見。"""
    out = []
    ok = [c for c in companies if c.get("metrics", {}).get("revenue")]
    if len(ok) >= 2:
        best = max((c for c in ok if c["metrics"].get("margin") is not None), key=lambda c: c["metrics"]["margin"], default=None)
        grow = max((c for c in ok if c["metrics"].get("cagr") is not None), key=lambda c: c["metrics"]["cagr"], default=None)
        if best:
            out.append(f"利益率トップは {best['name']}（{best['metrics']['profit_label']}率 {best['metrics']['margin']:.1%}）。")
        if grow:
            out.append(f"売上成長率トップは {grow['name']}（{grow['metrics']['cagr_years']}年CAGR {grow['metrics']['cagr']:.1%}）。")
        margins = [c["metrics"]["margin"] for c in ok if c["metrics"].get("margin") is not None]
        if margins:
            out.append(f"比較対象 {len(ok)} 社の利益率中央値は {statistics.median(margins):.1%}。")
    good = sorted([s for s in segs if s.get("margin") is not None and (s.get("growth") or 0) > 0], key=lambda s: -s["margin"])[:3]
    if good:
        out.append("高収益かつ成長しているセグメント: " + "、".join(f"{s['company']}「{s['segment']}」({s['margin']:.0%})" for s in good) + "。")
    bad = [s for s in segs if (s.get("profit") or 0) < 0]
    if bad:
        out.append("赤字セグメント: " + "、".join(f"{s['company']}「{s['segment']}」" for s in bad[:5]) + "。")
    return out


# ---------------------------------------------------------------- LLM
SYSTEM = (
    "あなたは日本企業のM&A・事業戦略を担当する経験豊富なアナリストです。"
    "与えられたデータだけを根拠に、事実と推測を区別して日本語で簡潔に分析します。"
    "データに無いことは書かず、不確かな点は『〜の可能性』と明示します。"
    "出力は指定のJSONのみ（前後に説明文やコードフェンスを付けない）。"
)


class LLM:
    def __init__(self, api_key: str | None = None, model: str | None = None, log=print):
        self.log = log
        self.client = None
        self.errors: list[str] = []
        self.model = model or os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5-5"
        key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=key, timeout=180, max_retries=3)
            except ImportError:
                log("  anthropic パッケージが無いためLLM要約をスキップします")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def ask_json(self, prompt: str, max_tokens: int = 4000) -> dict | None:
        if not self.client:
            return None
        try:
            res = self.client.messages.create(
                model=self.model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in res.content if getattr(b, "type", "") == "text")
            out = _parse_json(text)
            if out is None:
                self.errors.append("応答をJSONとして解釈できませんでした")
            return out
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            if "not_found" in msg or "model" in msg.lower() and "404" in msg:
                msg += f"（モデル名 {self.model} を確認してください。GitHubの Variables の CLAUDE_MODEL で変更できます）"
            self.errors.append(msg)
            self.log(f"  LLM呼び出し失敗: {msg}")
            return None


def _parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def _oku(v):
    return None if v is None else round(v / 1e8, 1)


def company_insight(llm: LLM, c: dict) -> dict | None:
    m = c["metrics"]
    data = {
        "会社名": c["name"], "業種": c.get("industry"), "決算期": m.get("period"),
        "会計基準": (c.get("annual") or {}).get("standard"),
        "業績推移(億円)": [{"期": t["period"], "売上": _oku(t["revenue"]), m["profit_label"]: _oku(t["profit"]),
                         "利益率": None if t["margin"] is None else round(t["margin"], 3)} for t in m["trend"]],
        "直近中間期": m.get("interim"),
        "セグメント(億円)": [{"名称": s["segment"], "売上": _oku(s.get("revenue")), "前期売上": _oku(s.get("revenue_prior")),
                          "利益": _oku(s.get("profit")), "前期利益": _oku(s.get("profit_prior"))} for s in m["segments"]],
    }
    texts = c.get("texts", {})
    excerpt = "\n\n".join(f"【{k}】\n{texts[k][:3500]}" for k in ("business", "policy", "mdna", "risks") if k in texts)
    prompt = f"""以下は有価証券報告書等（IR情報）から抽出したデータと記述です。
目的: どの事業が儲かっていて、どの事業が儲かっていないかを把握し、M&A・事業戦略の検討材料にする。

# 数値データ
{json.dumps(data, ensure_ascii=False, default=str)}

# 記述情報（抜粋）
{excerpt}

次のJSONで回答してください:
{{"summary": "事業と業績の要点（3文以内）",
 "profitable": [{{"business": "事業名", "evidence": "数値を含む根拠"}}],
 "struggling": [{{"business": "事業名", "evidence": "数値を含む根拠"}}],
 "strategy": ["会社が重点化/縮小を表明している領域"],
 "risks": ["主要リスク"],
 "ma_view": "買収・提携・事業売却の観点での論点（2〜3文）"}}"""
    return llm.ask_json(prompt, 2500)


def news_digest(llm: LLM, items: list[dict], subject: str) -> dict | None:
    if not items:
        return None
    lines = []
    for it in items[:120]:
        body = it.get("_body") or it.get("snippet") or ""
        lines.append(f"[{it['id']}] {it['date']} {it['source']} | {it['title']}" + (f"\n  {body[:600]}" if body else ""))
    prompt = f"""以下は「{subject}」に関するWebニュース・業界紙の見出し（一部は本文抜粋付き）です。
IR情報とは独立に、報道ベースで業界の動きを整理してください。見出しから読み取れる範囲に限定し、推測は推測と明記すること。

{chr(10).join(lines)}

次のJSONで回答してください（article_ids には根拠記事のIDを入れる）:
{{"overview": "全体像（3〜4文）",
 "themes": [{{"title": "テーマ", "summary": "2〜3文", "article_ids": ["n..."]}}],
 "growing": [{{"area": "伸びている事業・サービス領域", "evidence": "根拠", "article_ids": []}}],
 "declining": [{{"area": "苦戦している領域", "evidence": "根拠", "article_ids": []}}],
 "deals": [{{"summary": "M&A・提携・撤退などの動き", "article_ids": []}}]}}"""
    return llm.ask_json(prompt, 3500)


def synthesis(llm: LLM, subject: str, companies: list[dict], segs: list[dict], news: dict | None) -> dict | None:
    comp = [{"会社": c["name"], "売上(億円)": _oku(c["metrics"].get("revenue")),
             "利益率": c["metrics"].get("margin"), "CAGR": c["metrics"].get("cagr"),
             "要約": (c.get("insight") or {}).get("summary")} for c in companies if c.get("metrics")]
    sg = [{"会社": s["company"], "セグメント": s["segment"], "売上(億円)": _oku(s.get("revenue")),
           "利益率": s.get("margin"), "成長率": s.get("growth")} for s in segs]
    prompt = f"""対象: {subject}
## IR情報（有価証券報告書ベース）
企業別: {json.dumps(comp, ensure_ascii=False, default=str)}
セグメント別: {json.dumps(sg, ensure_ascii=False, default=str)}
## 報道（ニュース・業界紙の要約、IRとは別情報源）
{json.dumps(news or {}, ensure_ascii=False)[:6000]}

IRの数値を主な根拠、報道を補助情報として、どの事業・サービスが儲かっていて／儲かっていないかを整理してください。
根拠がIRか報道かを source に "IR" / "報道" / "IR+報道" で示すこと。セグメント利益の定義は会社ごとに異なる点に留意。
JSON:
{{"headline": "結論（2文）",
 "profitable": [{{"business": "事業・サービス類型", "why": "儲かる理由", "examples": "該当企業・セグメント", "source": "IR"}}],
 "unprofitable": [{{"business": "...", "why": "...", "examples": "...", "source": "IR"}}],
 "ir_vs_news": ["IR数値と報道の見立てが食い違う/先行している点"],
 "ma_implications": ["M&A・事業戦略への示唆"]}}"""
    return llm.ask_json(prompt, 3500)
