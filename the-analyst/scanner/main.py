"""CLI エントリーポイント。

例:
  python -m scanner.main --mode company  --target "トヨタ自動車,本田技研工業" --news-keywords "自動車"
  python -m scanner.main --mode industry --target "情報・通信業" --name-filter "システム,ソフト" --max-companies 10
  python -m scanner.main --mode check    # APIキーや外部サービスへの接続を診断
  python -m scanner.main --demo          # APIキー不要のデモ（架空データ）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sys
import traceback
from pathlib import Path

import yaml

from . import analyze
from .analyze import LLM, company_metrics, rule_highlights, segment_universe
from .news import collect_news, fetch_bodies
from .report import build_report, markdown_summary

ROOT = Path(__file__).resolve().parent.parent
JST = dt.timezone(dt.timedelta(hours=9))
MAX_COMPANIES = 30  # EDINETへの負荷と実行時間を抑える上限


def log(msg: str) -> None:
    print(msg, flush=True)


def gh_summary(text: str) -> None:
    """GitHub Actions の実行画面（Summary）に表示する。ローカル実行では何もしない。"""
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n\n")


def gh_output(**kv) -> None:
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def load_config(path: Path) -> dict:
    if path.exists():
        return yaml.safe_load(path.read_text("utf-8")) or {}
    return {}


def split_list(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,、，\n]+", s or "") if x.strip()]


def short_name(name: str) -> str:
    return re.sub(r"(株式会社|ホールディングス|（株）|\(株\))", "", name).strip()


def model_name(args, cfg) -> str:
    # 優先順位: --model > 環境変数 CLAUDE_MODEL（GitHubの Variables）> config.yaml > 既定
    return args.model or os.environ.get("CLAUDE_MODEL", "").strip() or (cfg.get("llm") or {}).get("model") or "claude-sonnet-5-5"


def edinet_client():
    from .edinet import EdinetClient

    cache = Path(os.environ.get("SCANNER_CACHE_DIR") or ROOT / ".cache") / "edinet"
    return EdinetClient(os.environ.get("EDINET_API_KEY"), cache_dir=cache, log=log,
                        sleep=float(os.environ.get("EDINET_SLEEP", "1.0")))


# ---------------------------------------------------------------- IR取得
def collect_ir(args) -> tuple[list[dict], list[str], str]:
    from .edinet import DOC_ANNUAL, DOC_INTERIM
    from .xbrl_parse import extract_financials, extract_segments, extract_texts, member_labels_from_xbrl_zip, read_csv_zip

    client = edinet_client()
    warnings: list[str] = []
    if args.mode == "industry":
        log(f"▶ 業種「{args.target}」の企業を抽出")
        companies = client.companies_by_industry(args.target, args.max_companies, args.name_filter)
        subject = args.target + (f"（{args.name_filter}）" if args.name_filter else "")
    else:
        log(f"▶ 企業を特定: {args.target}")
        companies, warnings = client.resolve_companies(split_list(args.target)[:args.max_companies])
        subject = "、".join(c["name"] for c in companies)
    for w in warnings:
        log("  ⚠ " + w)
    if not companies:
        raise RuntimeError("対象企業が見つかりません: " + " / ".join(warnings))
    log("  対象: " + "、".join(c["name"] for c in companies))

    log(f"▶ EDINETで提出書類を検索（最大 {args.lookback_days} 日遡り。初回は数分かかります）")
    filings = client.find_latest_filings([c["edinet_code"] for c in companies], args.lookback_days)

    for c in companies:
        f = filings.get(c["edinet_code"], {})
        c["errors"] = []
        ann, itm = f.get(DOC_ANNUAL), f.get("interim")
        if not ann:
            msg = f"直近{args.lookback_days}日以内にCSV付きの有価証券報告書が見つかりません"
            c["errors"].append(msg)
            warnings.append(f"{c['name']}: {msg}")
            continue
        log(f"▶ {c['name']}: 有報 {ann['docID']}（{ann.get('periodEnd')}期）" + (f" ／ 中間 {itm['docID']}" if itm else ""))
        try:
            df = read_csv_zip(client.download(ann["docID"], 5))
            fin = extract_financials(df, annual=True)
            labels = {}
            try:
                labels = member_labels_from_xbrl_zip(client.download(ann["docID"], 1))
            except Exception as e:  # セグメント名が英語表記になるだけなので続行
                c["errors"].append(f"セグメント名（日本語）の取得に失敗: {e}")
            c["annual"] = {**fin, "doc_id": ann["docID"],
                           "period_end": ann.get("periodEnd") or fin["dei"].get("period_end", ""),
                           "submit_date": (ann.get("submitDateTime") or "")[:10], "description": ann.get("docDescription")}
            c["segments"] = extract_segments(df, labels, annual=True)
            c["texts"] = extract_texts(df)
            if not fin["series"].get("revenue"):
                warnings.append(f"{c['name']}: 売上高を特定できませんでした（特殊な様式の可能性）")
        except Exception as e:
            c["errors"].append(f"有報の解析エラー: {e}")
            warnings.append(f"{c['name']}: 有報の解析エラー: {e}")
            traceback.print_exc()
            continue
        if itm:
            try:
                dfi = read_csv_zip(client.download(itm["docID"], 5))
                fi = extract_financials(dfi, annual=False)
                c["interim"] = {**fi, "doc_id": itm["docID"],
                                "period_end": itm.get("periodEnd") or fi["dei"].get("period_end", ""),
                                "submit_date": (itm.get("submitDateTime") or "")[:10],
                                "doc_label": "半期報告書" if str(itm.get("docTypeCode")) == DOC_INTERIM else "四半期報告書"}
            except Exception as e:
                c["errors"].append(f"中間期の解析エラー: {e}")
        c["metrics"] = company_metrics(c)
    log(f"  EDINET API 呼び出し回数: {client.calls}")
    return companies, warnings, subject


# ---------------------------------------------------------------- 実行
def run(args) -> Path:
    cfg = load_config(Path(args.config))
    ncfg = cfg.get("news") or {}
    llm = LLM(model=model_name(args, cfg), log=log)
    if args.no_llm:
        llm.client = None
    log(f"▶ LLM要約: {'有効（' + llm.model + '）' if llm.enabled else '無効（ANTHROPIC_API_KEY 未設定）'}")

    if args.demo:
        from .demo import demo_companies, demo_news
        companies, warnings, subject = demo_companies(), [], "【デモ】SaaS・ITサービス業界"
        for c in companies:
            c["metrics"] = company_metrics(c)
    else:
        companies, warnings, subject = collect_ir(args)

    ok = [c for c in companies if c.get("metrics")]
    if not ok:
        raise RuntimeError("どの企業のIR情報も取得できませんでした: " + " / ".join(warnings))

    # ---- ニュース（IRとは別系統）
    log("▶ ニュース・業界紙を収集")
    topic_kw = split_list(args.news_keywords)
    if not topic_kw and args.mode == "industry":
        topic_kw = [re.sub(r"(業|、.*)$", "", args.target.replace("・", " ")).strip() or args.target]
    queries = []
    for k in topic_kw:
        queries += [f"{k} 業界 動向", f"{k} 市場 拡大 OR 縮小", f"{k} 買収 OR 提携 OR 撤退"]
    top = sorted(ok, key=lambda c: -(c["metrics"].get("revenue") or 0))[: ncfg.get("company_queries", 6)]
    for c in top:
        s = short_name(c["name"])
        if len(ok) > 1:
            queries.append(f'"{s}" 事業')
        else:
            queries += [f'"{s}"', f'"{s}" 決算', f'"{s}" 買収 OR 提携']
    queries += ncfg.get("extra_queries") or []
    keywords = topic_kw + [short_name(c["name"]) for c in ok]
    if args.demo:
        news = demo_news()
    else:
        news = collect_news(queries, keywords, ncfg.get("feeds") or [], days=args.news_days,
                            per_query=ncfg.get("per_query", 25), max_total=ncfg.get("max_total", 150), log=log)
        for e in news["errors"]:
            log("  ⚠ " + e)
        if not news["items"] and news["errors"]:
            warnings.append("ニュースを取得できませんでした: " + " / ".join(news["errors"][:3]))
        if llm.enabled and ncfg.get("fetch_bodies", True):
            fetch_bodies(news["items"], limit=ncfg.get("body_limit", 30))

    # ---- 分析
    segs = segment_universe(ok)
    news_dg = syn = None
    if llm.enabled:
        log(f"▶ LLMで要約（{llm.model}）")
        for c in ok:
            c["insight"] = analyze.company_insight(llm, c)
        news_dg = analyze.news_digest(llm, news["items"], subject)
        syn = analyze.synthesis(llm, subject, ok, segs, news_dg)
        if llm.errors:
            warnings.append(f"LLM要約の一部が失敗しました（{llm.errors[0]}）")
    for it in news["items"]:
        it.pop("_body", None)  # 記事本文は保存しない

    now = dt.datetime.now(JST)
    slug = now.strftime("%Y%m%d-%H%M") + "-" + ("demo" if args.demo else args.mode) + "-" + hashlib.sha1(subject.encode()).hexdigest()[:6]
    result = {
        "title": f"{subject} — 事業・業績スキャン", "slug": slug, "generated_at": now.strftime("%Y-%m-%d %H:%M JST"),
        "mode": "demo" if args.demo else args.mode, "demo": bool(args.demo), "subject": subject,
        "params": {"mode": args.mode, "target": args.target, "name_filter": args.name_filter,
                   "news_keywords": args.news_keywords, "news_days": args.news_days,
                   "max_companies": args.max_companies, "lookback_days": args.lookback_days},
        "llm_enabled": llm.enabled, "llm_model": llm.model if llm.enabled else None,
        "companies": companies, "news": news, "news_digest": news_dg, "synthesis": syn,
        "highlights": rule_highlights(ok, segs), "warnings": warnings,
        "request_id": getattr(args, "request_id", None),
    }
    out = build_report(result, (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out))
    log(f"✔ レポート出力: {out / 'index.html'}")
    gh_summary(markdown_summary(result, ok, segs))
    try:
        rel = out.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        rel = out.resolve().as_posix()
    gh_output(report_dir=rel, slug=slug)
    args._result = {"report_dir": rel, "slug": slug, "title": result["title"], "warnings": warnings,
                    "companies": [c["name"] for c in ok], "llm_enabled": llm.enabled}
    return out


# ---------------------------------------------------------------- 接続診断
def run_check(args) -> bool:
    """APIキー・外部サービスへの接続を確認し、結果を表で出す。"""
    from .edinet import today_jst
    from .news import custom_feed, google_news

    cfg = load_config(Path(args.config))
    rows: list[tuple[str, bool | None, str]] = []

    def add(name, ok, detail):
        rows.append((name, ok, detail))
        log(f"  {'✅' if ok else ('⚪' if ok is None else '❌')} {name}: {detail}")

    log("▶ 接続診断")
    try:
        client = edinet_client()
        day = today_jst() - dt.timedelta(days=1)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        docs = client.documents_on(day)
        add("EDINET API（書類一覧）", True, f"{day} の提出書類 {len(docs)} 件を取得")
        try:
            n = len(client.load_codelist())
            add("EDINET コードリスト", True, f"{n} 社")
            if args.target:
                cs, ws = client.resolve_companies(split_list(args.target))
                add("企業名の解決", bool(cs), "、".join(f"{c['name']}({c['sec_code']})" for c in cs) + (" ／ " + " ".join(ws) if ws else ""))
        except Exception as e:
            add("EDINET コードリスト", False, str(e))
    except Exception as e:
        add("EDINET API（書類一覧）", False, str(e))

    try:
        items = google_news("決算", 7)
        add("Googleニュース RSS", bool(items), f"{len(items)} 件")
    except Exception as e:
        add("Googleニュース RSS", False, f"{type(e).__name__}: {e}")
    for f in (cfg.get("news") or {}).get("feeds") or []:
        try:
            items = custom_feed(f.get("name", f["url"]), f["url"], [], 30)
            add(f"業界紙RSS: {f.get('name', f['url'])}", True, f"{len(items)} 件（30日以内）")
        except Exception as e:
            add(f"業界紙RSS: {f.get('name', f.get('url'))}", False, f"{type(e).__name__}: {e}")

    llm = LLM(model=model_name(args, cfg), log=log)
    if not llm.enabled:
        add("Claude API", None, "ANTHROPIC_API_KEY 未設定（LLM要約なしで動作します）")
    else:
        res = llm.ask_json('{"ok": true} とだけ返してください。', 50)
        add(f"Claude API（{llm.model}）", bool(res), "応答OK" if res else (llm.errors[-1] if llm.errors else "応答なし"))

    md = ["## 接続診断", "", "| 項目 | 結果 | 詳細 |", "|---|---|---|"]
    md += [f"| {n} | {'✅' if ok else ('⚪ 任意' if ok is None else '❌')} | {d.replace('|', '／')} |" for n, ok, d in rows]
    gh_summary("\n".join(md))
    args._result = {"check": [{"item": n, "ok": ok, "detail": d} for n, ok, d in rows]}
    return all(ok is not False for _, ok, _ in rows)


# ---------------------------------------------------------------- 依頼ファイル（Claude等からの実行）
REQUEST_KEYS = {"mode": str, "target": str, "name_filter": str, "news_keywords": str,
                "max_companies": int, "news_days": int, "lookback_days": int}


def apply_request(args, path: Path) -> None:
    """requests/<id>.json の内容で引数を上書きする。未知のキーは無視。"""
    import json
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSONオブジェクトではありません")
    for k, typ in REQUEST_KEYS.items():
        if k in data and data[k] is not None and data[k] != "":
            setattr(args, k, typ(data[k]))
    if args.mode not in ("company", "industry", "check"):
        raise ValueError(f"mode が不正です: {args.mode}")
    args.request_id = path.stem
    args.request_path = path
    args.max_companies = max(1, min(args.max_companies, MAX_COMPANIES))
    args.news_days = max(1, min(args.news_days, 365))
    args.lookback_days = max(30, min(args.lookback_days, 800))


def write_result(args, status: str, error: str | None = None) -> None:
    """依頼ファイルの横に <id>.result.json を書く（成功・失敗とも）。依頼ファイル経由でなければ何もしない。"""
    import json
    path = getattr(args, "request_path", None) or (Path(args.request) if getattr(args, "request", "") else None)
    if not path:
        return
    res = {"request_id": path.stem, "status": status, "error": error, "mode": args.mode, "target": args.target,
           "finished_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
           "run_url": (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                       if os.environ.get("GITHUB_RUN_ID") else None)}
    res.update(getattr(args, "_result", {}) or {})
    out = path.with_name(path.stem + ".result.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    log(f"  結果ファイル: {out}")


def main(argv=None):
    p = argparse.ArgumentParser(description="IR情報＋業界ニュースで事業の収益性をスキャン")
    p.add_argument("--mode", choices=["company", "industry", "check"], default="company")
    p.add_argument("--target", default="", help="会社名/証券コード（カンマ区切り）または業種名（東証33業種）")
    p.add_argument("--name-filter", default="", help="industryモードで社名を絞り込むキーワード（カンマ区切り）")
    p.add_argument("--news-keywords", default="", help="ニュース検索の業界キーワード（例: SaaS,クラウド会計）")
    p.add_argument("--max-companies", type=int, default=12)
    p.add_argument("--news-days", type=int, default=90)
    p.add_argument("--lookback-days", type=int, default=400, help="EDINETを遡る日数（有報は年1回のため400日程度）")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--out", default="docs/reports")
    p.add_argument("--model", default=None, help="Claudeのモデル名（既定: CLAUDE_MODEL → config.yaml → claude-sonnet-5-5）")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--demo", action="store_true", help="APIキー不要のデモ（架空データ）")
    p.add_argument("--request", default="", help="依頼ファイル（requests/<id>.json）。Claudeなどからの実行用")
    args = p.parse_args(argv)
    args.request_id = None
    if args.request:
        try:
            apply_request(args, Path(args.request))
        except Exception as e:
            log(f"✖ 依頼ファイルを読めません: {e}")
            write_result(args, "error", error=f"依頼ファイルを読めません: {e}")
            return 1
    args.max_companies = max(1, min(args.max_companies, MAX_COMPANIES))
    args.news_days = max(1, min(args.news_days, 365))
    args.lookback_days = max(30, min(args.lookback_days, 800))

    if args.mode == "check":
        ok = run_check(args)
        write_result(args, "ok" if ok else "error", error=None if ok else "接続診断で失敗した項目があります（check を参照）")
        return 0 if ok else 1
    if not args.demo and not args.target.strip():
        if args.request:
            write_result(args, "error", error="target（会社名・証券コード・業種名）が指定されていません")
            return 1
        p.error("--target を指定してください")
    try:
        run(args)
        write_result(args, "ok")
    except Exception as e:
        log(f"✖ エラー: {e}")
        write_result(args, "error", error=str(e))
        gh_summary(f"## ❌ 実行に失敗しました\n\n{e}\n\n"
                   "APIキーや接続に問題がありそうな場合は、mode に `check` を選んで実行すると原因を確認できます。")
        if not isinstance(e, (RuntimeError,)) or os.environ.get("SCANNER_DEBUG"):
            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
