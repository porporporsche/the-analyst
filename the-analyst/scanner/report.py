"""HTMLレポート / Markdownサマリー / JSONデータの出力。"""
from __future__ import annotations

import datetime as dt
import html
import json
import statistics
from pathlib import Path

import plotly.graph_objects as go
from jinja2 import Environment, FileSystemLoader, select_autoescape
from plotly.offline import get_plotlyjs_version

from .analyze import segment_universe
from .edinet import DOC_VIEW_URL
from .xbrl_parse import TEXT_KEYS

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
BLUE, BLUE_LIGHT, GRAY, LOSS = "#2a78d6", "#9ec5f4", "#8c8b86", "#e34948"
FONT = "'Hiragino Sans','Noto Sans JP','Yu Gothic UI',Meiryo,sans-serif"


# ---------------------------------------------------------------- 書式
def oku(v, digits=0):
    if v is None:
        return "—"
    x = v / 1e8
    return f"{x:,.{digits}f}" if abs(x) >= 10 or digits else f"{x:,.1f}"


def pct(v, digits=1, sign=False):
    if v is None:
        return "—"
    return f"{v:+.{digits}%}" if sign else f"{v:.{digits}%}"


def _layout(fig, title="", height=360, **kw):
    fig.update_layout(
        title=dict(text=title, font=dict(size=14), x=0, xanchor="left"),
        height=height, margin=dict(l=8, r=8, t=40 if title else 10, b=8),
        font=dict(family=FONT, size=12, color="#3b3a37"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(font=dict(family=FONT)), legend=dict(orientation="h", y=-0.15),
        **kw,
    )
    fig.update_xaxes(showgrid=False, linecolor="#d6d5d0", automargin=True)
    fig.update_yaxes(gridcolor="#ecebe7", zerolinecolor="#b9b8b2", automargin=True)
    return fig


def _html(fig) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False, "responsive": True})


# ---------------------------------------------------------------- 図
def fig_company_trend(c) -> tuple[str, str]:
    t = c["metrics"]["trend"]  # 古い期→新しい期
    x = [r["period"] for r in t]
    f1 = go.Figure()
    f1.add_bar(x=x, y=[(r["revenue"] or 0) / 1e8 for r in t], name="売上", marker_color=BLUE_LIGHT,
               hovertemplate="%{x}<br>売上 %{y:,.0f}億円<extra></extra>")
    f1.add_bar(x=x, y=[(r["profit"] or 0) / 1e8 if r["profit"] is not None else None for r in t],
               name=c["metrics"]["profit_label"], marker_color=BLUE,
               hovertemplate="%{x}<br>" + c["metrics"]["profit_label"] + " %{y:,.0f}億円<extra></extra>")
    _layout(f1, "売上・利益の推移（億円）", 300, barmode="group", bargap=0.35, bargroupgap=0.08)
    f2 = go.Figure()
    f2.add_scatter(x=x, y=[r["margin"] for r in t], mode="lines+markers+text", line=dict(color=BLUE, width=2),
                   marker=dict(size=8), text=[pct(r["margin"]) for r in t], textposition="top center",
                   hovertemplate="%{x}<br>利益率 %{y:.1%}<extra></extra>", name="利益率")
    _layout(f2, f"{c['metrics']['profit_label']}率の推移", 300, showlegend=False)
    f2.update_yaxes(tickformat=".0%", rangemode="tozero")
    return _html(f1), _html(f2)


def fig_company_segments(c) -> str | None:
    segs = [s for s in c["metrics"]["segments"] if s.get("revenue")]
    if not segs:
        return None
    segs = sorted(segs, key=lambda s: s["revenue"])
    y = [s["segment"] for s in segs]
    f = go.Figure()
    f.add_bar(y=y, x=[(s.get("revenue_prior") or 0) / 1e8 for s in segs], orientation="h", name="前期売上",
              marker_color="#cfcec8", hovertemplate="%{y}<br>前期売上 %{x:,.0f}億円<extra></extra>")
    f.add_bar(y=y, x=[s["revenue"] / 1e8 for s in segs], orientation="h", name="当期売上", marker_color=BLUE,
              text=[f"利益率 {pct(s.get('margin'))}" for s in segs], textposition="outside",
              hovertemplate="%{y}<br>当期売上 %{x:,.0f}億円<extra></extra>")
    _layout(f, "セグメント別売上（億円）と利益率", max(200, 90 + 44 * len(segs)), barmode="group", bargap=0.3)
    top = max(max(s["revenue"], s.get("revenue_prior") or 0) for s in segs) / 1e8
    f.update_xaxes(range=[0, top * 1.22])
    return _html(f)


def fig_company_bubble(companies) -> str | None:
    pts = [c for c in companies if c["metrics"].get("margin") is not None and c["metrics"].get("revenue")]
    if len(pts) < 2:
        return None
    gx = [c["metrics"].get("cagr") if c["metrics"].get("cagr") is not None else c["metrics"].get("yoy") for c in pts]
    rev = [c["metrics"]["revenue"] for c in pts]
    mx = max(rev)
    f = go.Figure()
    f.add_scatter(
        x=gx, y=[c["metrics"]["margin"] for c in pts], mode="markers+text",
        text=[c["name"] for c in pts], textposition="top center",
        marker=dict(size=[12 + 48 * (r / mx) ** 0.5 for r in rev], color=BLUE, opacity=0.55,
                    line=dict(color="#ffffff", width=2)),
        customdata=[[oku(c["metrics"]["revenue"]), c["metrics"]["profit_label"]] for c in pts],
        hovertemplate="<b>%{text}</b><br>売上 %{customdata[0]}億円<br>成長率 %{x:.1%}<br>%{customdata[1]}率 %{y:.1%}<extra></extra>",
    )
    _quadrants(f, gx, [c["metrics"]["margin"] for c in pts])
    _layout(f, "成長性 × 収益性（円の大きさ＝売上）", 460, showlegend=False)
    f.update_xaxes(title="売上成長率（CAGR、無い場合は前年比）", tickformat=".0%", zeroline=True)
    f.update_yaxes(title="利益率", tickformat=".0%")
    return _html(f)


def fig_segment_bubble(segs) -> str | None:
    pts = [s for s in segs if s.get("margin") is not None and s.get("growth") is not None]
    if len(pts) < 3:
        return None
    mx = max(s["revenue"] for s in pts)
    f = go.Figure()
    for loss in (False, True):
        sub = [s for s in pts if (s["profit"] < 0) == loss]
        if not sub:
            continue
        f.add_scatter(
            x=[s["growth"] for s in sub], y=[s["margin"] for s in sub], mode="markers",
            name="赤字セグメント" if loss else "黒字セグメント",
            marker=dict(size=[8 + 40 * (s["revenue"] / mx) ** 0.5 for s in sub], color=LOSS if loss else BLUE,
                        opacity=0.55, line=dict(color="#ffffff", width=2),
                        symbol="diamond" if loss else "circle"),
            text=[f"{s['company']}／{s['segment']}" for s in sub],
            customdata=[oku(s["revenue"]) for s in sub],
            hovertemplate="<b>%{text}</b><br>売上 %{customdata}億円<br>成長率 %{x:.1%}<br>利益率 %{y:.1%}<extra></extra>",
        )
    # 上位のみ直接ラベル
    top = sorted(pts, key=lambda s: -s["revenue"])[:10]
    for s in top:
        f.add_annotation(x=s["growth"], y=s["margin"], text=s["segment"][:12], showarrow=False, yshift=14,
                         font=dict(size=10, color="#52514e"))
    _quadrants(f, [s["growth"] for s in pts], [s["margin"] for s in pts])
    _layout(f, "事業セグメントの成長性 × 収益性（円の大きさ＝セグメント売上）", 520)
    f.update_xaxes(title="セグメント売上成長率（前年比）", tickformat=".0%")
    f.update_yaxes(title="セグメント利益率", tickformat=".0%")
    return _html(f)


def fig_revenue_lines(companies) -> str | None:
    cs = sorted([c for c in companies if len(c["metrics"].get("trend", [])) >= 2],
                key=lambda c: -(c["metrics"].get("revenue") or 0))[:8]
    if len(cs) < 2:
        return None
    f = go.Figure()
    for i, c in enumerate(cs):
        t = [r for r in c["metrics"]["trend"] if r["revenue"]]
        base = t[0]["revenue"]
        f.add_scatter(x=[_fy(r["period"]) for r in t], y=[r["revenue"] / base * 100 for r in t], name=c["name"],
                      mode="lines+markers", line=dict(color=SERIES[i], width=2), marker=dict(size=8),
                      customdata=[r["period"] for r in t],
                      hovertemplate=c["name"] + "<br>%{customdata}: %{y:.0f}<extra></extra>")
    _layout(f, "売上高の推移（各社の最古期＝100として指数化、売上上位8社）", 380)
    # 決算月が異なる会社（3月決算と12月決算など）も年の数値軸で正しく並べる
    f.update_xaxes(title="決算期（年）", type="linear", dtick=1, tickformat="d")
    return _html(f)


def _pad(f, xs, ys, px=0.15, py=0.12):
    xs, ys = [x for x in xs if x is not None], [y for y in ys if y is not None]
    if xs and ys:
        dx, dy = (max(xs) - min(xs)) or 0.1, (max(ys) - min(ys)) or 0.1
        f.update_xaxes(range=[min(xs) - dx * px, max(xs) + dx * px])
        f.update_yaxes(range=[min(ys) - dy * py, max(ys) + dy * py])


def _fy(period: str) -> float | None:
    """'2024/03期' → 2024.25（決算月を年の小数で表し、横軸上で時系列順に並べる）"""
    try:
        return int(period[:4]) + (int(period[5:7]) - 1) / 12
    except (TypeError, ValueError):
        return None


def _quadrants(f, xs, ys):
    _pad(f, xs, ys)
    xs, ys = [x for x in xs if x is not None], [y for y in ys if y is not None]
    if not xs or not ys:
        return
    mx, my = statistics.median(xs), statistics.median(ys)
    f.add_vline(x=mx, line=dict(color="#b9b8b2", width=1, dash="dot"))
    f.add_hline(y=my, line=dict(color="#b9b8b2", width=1, dash="dot"))
    f.add_annotation(xref="paper", yref="paper", x=1, y=1, text="高成長・高収益", showarrow=False,
                     font=dict(size=11, color="#8c8b86"), xanchor="right", yanchor="top")
    f.add_annotation(xref="paper", yref="paper", x=0, y=0, text="低成長・低収益", showarrow=False,
                     font=dict(size=11, color="#8c8b86"), xanchor="left", yanchor="bottom")


# ---------------------------------------------------------------- 出力
def build_report(result: dict, out_root: Path) -> Path:
    companies = sorted([c for c in result["companies"] if c.get("metrics")],
                       key=lambda c: -(c["metrics"].get("revenue") or 0))
    segs = segment_universe(companies)
    multi = len(companies) >= 2
    for c in companies:
        c["_trend_figs"] = fig_company_trend(c) if c["metrics"].get("trend") else None
        c["_seg_fig"] = fig_company_segments(c)
        a = c.get("annual") or {}
        c["_doc_url"] = DOC_VIEW_URL.format(doc_id=a["doc_id"]) if a.get("doc_id") else None
        it = c.get("interim") or {}
        c["_interim_url"] = DOC_VIEW_URL.format(doc_id=it["doc_id"]) if it.get("doc_id") else None
    ctx = {
        "r": result, "companies": companies, "multi": multi, "segs": segs,
        "fig_bubble": fig_company_bubble(companies) if multi else None,
        "fig_lines": fig_revenue_lines(companies) if multi else None,
        "fig_seg": fig_segment_bubble(segs),
        "seg_good": sorted([s for s in segs if (s.get("margin") or 0) > 0 and (s.get("growth") or 0) >= 0],
                           key=lambda s: -s["margin"])[:12],
        "seg_bad": sorted([s for s in segs if (s.get("profit") or 0) < 0 or (s.get("growth") or 0) < -0.05],
                          key=lambda s: (s.get("margin") if s.get("margin") is not None else 0))[:12],
        "news_by_id": {n["id"]: n for n in result["news"]["items"]},
        "text_titles": {k: v[1] for k, v in TEXT_KEYS.items()},
        "plotly_js": f"https://cdn.jsdelivr.net/npm/plotly.js-dist-min@{get_plotlyjs_version()}/plotly.min.js",
        "oku": oku, "pct": pct,
    }
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                      autoescape=select_autoescape(["html"]))
    out = out_root / result["slug"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(env.get_template("report.html.j2").render(**ctx), "utf-8")
    (out / "report.md").write_text(markdown_summary(result, companies, segs), "utf-8")
    (out / "data.json").write_text(json.dumps(_clean_for_json(result), ensure_ascii=False, indent=1, default=str), "utf-8")
    (out / "compact.json").write_text(json.dumps(compact_result(result, companies), ensure_ascii=False, default=str), "utf-8")
    (out / "meta.json").write_text(json.dumps({
        "title": result["title"], "generated_at": result["generated_at"], "mode": result["mode"],
        "companies": [c["name"] for c in companies], "demo": result.get("demo", False),
        "request_id": result.get("request_id"),
    }, ensure_ascii=False), "utf-8")
    write_index(out_root)
    return out


def compact_result(result: dict, companies: list[dict]) -> dict:
    """Artifact（claude.ai のページ）に取り込む用の縮約版。有報の原文抜粋など大きい項目は除く（256KB未満）。"""
    def r(v, n=4):
        return None if v is None else round(float(v), n)

    out_c = []
    for c in companies:
        m = c.get("metrics") or {}
        a = c.get("annual") or {}
        out_c.append({
            "name": c["name"], "sec_code": c.get("sec_code"), "industry": c.get("industry"),
            "standard": a.get("standard"), "basis": a.get("basis"), "submit_date": a.get("submit_date"),
            "doc_url": DOC_VIEW_URL.format(doc_id=a["doc_id"]) if a.get("doc_id") else None,
            "period": m.get("period"), "profit_label": m.get("profit_label"),
            "revenue": m.get("revenue"), "profit": m.get("profit"), "net_income": m.get("net_income"),
            "margin": r(m.get("margin")), "margin_prev": r(m.get("margin_prev")), "yoy": r(m.get("yoy")),
            "cagr": r(m.get("cagr")), "cagr_years": m.get("cagr_years"), "roe": r(m.get("roe")),
            "employees": m.get("employees"), "interim": m.get("interim"),
            "trend": [{k: t.get(k) for k in ("period", "revenue", "profit", "margin")} for t in m.get("trend", [])],
            "segments": [{k: s.get(k) for k in ("segment", "revenue", "revenue_prior", "profit", "profit_prior",
                                                  "margin", "growth", "share")} for s in m.get("segments", [])],
            "insight": c.get("insight"), "errors": c.get("errors") or [],
        })
    news = result.get("news") or {}
    return {
        "title": result["title"], "subject": result.get("subject"), "generated_at": result["generated_at"],
        "slug": result["slug"], "mode": result.get("mode"), "params": result.get("params"),
        "request_id": result.get("request_id"), "llm_enabled": result.get("llm_enabled"),
        "companies": out_c, "highlights": result.get("highlights") or [], "warnings": result.get("warnings") or [],
        "synthesis": result.get("synthesis"), "news_digest": result.get("news_digest"),
        "news": {"queries": news.get("queries", []), "items": [
            {k: it.get(k) for k in ("id", "title", "link", "source", "date", "channel")} for it in news.get("items", [])[:120]]},
    }


def _clean_for_json(result):
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if not str(k).startswith("_")}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return strip(result)


def markdown_summary(result, companies, segs) -> str:
    L = [f"# {result['title']}", "", f"生成: {result['generated_at']}", ""]
    syn = result.get("synthesis") or {}
    if syn.get("headline"):
        L += [f"> {syn['headline']}", ""]
    for h in result.get("highlights", []):
        L.append(f"- {h}")
    L += ["", "## 企業比較（IR情報）", "", "| 会社 | 決算期 | 売上(億円) | 利益率 | 前年比 | CAGR |", "|---|---|--:|--:|--:|--:|"]
    for c in sorted(companies, key=lambda c: -(c["metrics"].get("revenue") or 0)):
        m = c["metrics"]
        L.append(f"| {c['name']} | {m.get('period','')} | {oku(m.get('revenue'))} | {pct(m.get('margin'))} | {pct(m.get('yoy'), sign=True)} | {pct(m.get('cagr'), sign=True)} |")
    if segs:
        L += ["", "## 利益率の高いセグメント", "", "| 会社 | セグメント | 売上(億円) | 利益率 | 成長率 |", "|---|---|--:|--:|--:|"]
        for s in sorted([s for s in segs if s.get("margin") is not None], key=lambda s: -s["margin"])[:10]:
            L.append(f"| {s['company']} | {s['segment']} | {oku(s['revenue'])} | {pct(s['margin'])} | {pct(s.get('growth'), sign=True)} |")
    nd = result.get("news_digest") or {}
    L += ["", "## ニュース・業界紙（IRとは別情報源）", "", nd.get("overview") or f"{len(result['news']['items'])} 件の記事を収集。", ""]
    if result.get("warnings"):
        L += ["", "## 警告", ""] + [f"- {w}" for w in result["warnings"]]
    return "\n".join(L) + "\n"


def write_index(out_root: Path) -> None:
    rows = []
    prefix = "reports/" if out_root.name == "reports" else ""
    for meta in sorted(out_root.glob("*/meta.json"), reverse=True):
        try:
            m = json.loads(meta.read_text("utf-8"))
        except Exception:
            continue
        rows.append(
            f"<tr><td>{html.escape(m['generated_at'][:16])}</td><td><a href='{prefix}{meta.parent.name}/index.html'>"
            f"{html.escape(m['title'])}</a>{' <span class=demo>デモ</span>' if m.get('demo') else ''}</td>"
            f"<td>{html.escape('、'.join(m.get('companies', [])[:6]))}{' ほか' if len(m.get('companies', [])) > 6 else ''}</td></tr>"
        )
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IRスキャン レポート一覧</title>
<style>body{{font-family:{FONT};margin:0;background:#f7f6f3;color:#1d1c1a}}main{{max-width:960px;margin:0 auto;padding:32px 16px}}
h1{{font-size:22px}}table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
td,th{{padding:10px 12px;border-bottom:1px solid #ecebe7;text-align:left;font-size:14px}}th{{background:#f0efeb;font-weight:600}}
a{{color:#1c5cab}}.demo{{font-size:11px;background:#fdecc8;padding:1px 6px;border-radius:4px}}</style></head>
<body><main><h1>IRスキャン レポート一覧</h1><table><tr><th>生成日時</th><th>レポート</th><th>対象企業</th></tr>
{''.join(rows) or '<tr><td colspan=3>まだレポートがありません</td></tr>'}</table></main></body></html>"""
    (out_root.parent / "index.html" if out_root.name == "reports" else out_root / "index.html").write_text(page, "utf-8")
