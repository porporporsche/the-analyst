"""Webニュース・業界紙の収集（RSS）。IR情報とは別系統で扱う。

- Google ニュース RSS 検索（APIキー不要）
- config.yaml の news.feeds に登録した業界紙・専門メディアのRSS（キーワードで絞り込み）
記事本文はレポートに転載しない。LLM要約の入力にのみ一時的に使用する。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

GNEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ir-industry-scanner/1.0)"}


def _clean(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _date(entry) -> str:
    for k in ("published", "updated"):
        v = entry.get(k)
        if v:
            try:
                return parsedate_to_datetime(v).date().isoformat()
            except Exception:
                pass
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            return dt.date(*v[:3]).isoformat()
    return ""


def _fetch_feed(url: str):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return feedparser.parse(r.content)


def google_news(query: str, days: int) -> list[dict]:
    q = f"{query} when:{days}d"
    feed = _fetch_feed(GNEWS.format(q=quote_plus(q)))
    out = []
    for e in feed.entries:
        title = _clean(e.get("title", ""))
        source = _clean((e.get("source") or {}).get("title", ""))
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append({
            "title": title, "link": e.get("link", ""), "source": source or "Google News",
            "date": _date(e), "snippet": "", "query": query, "channel": "Webニュース",
        })
    return out


def custom_feed(name: str, url: str, keywords: list[str], days: int) -> list[dict]:
    feed = _fetch_feed(url)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    kws = [k.lower() for k in keywords if k]
    out = []
    for e in feed.entries:
        title, summ = _clean(e.get("title", "")), _clean(e.get("summary", ""))
        d = _date(e)
        if d and d < since:
            continue
        text = (title + " " + summ).lower()
        hit = [k for k in kws if k in text]
        if kws and not hit:
            continue
        out.append({
            "title": title, "link": e.get("link", ""), "source": name, "date": d,
            "snippet": summ[:300], "query": ",".join(hit), "channel": "業界紙・専門メディア",
        })
    return out


def collect_news(queries: list[str], keywords: list[str], feeds: list[dict], days: int = 90,
                 per_query: int = 25, max_total: int = 150, log=print) -> dict:
    items, errors = [], []
    for q in queries:
        try:
            items += google_news(q, days)[:per_query]
        except Exception as e:  # 1クエリの失敗で全体を止めない
            errors.append(f"Googleニュース「{q}」: {e}")
        time.sleep(1.0)
    for f in feeds or []:
        try:
            items += custom_feed(f.get("name", f["url"]), f["url"], keywords, days)
        except Exception as e:
            errors.append(f"RSS {f.get('name', f.get('url'))}: {e}")
    # 重複除去（タイトル正規化）
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        key = re.sub(r"[\s\W]+", "", it["title"])[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        it["id"] = "n" + hashlib.sha1((it["link"] or key).encode()).hexdigest()[:8]
        uniq.append(it)
    log(f"  ニュース {len(uniq)} 件（重複除去後）")
    return {"items": uniq[:max_total], "queries": queries, "feeds": [f.get("name") for f in feeds or []], "errors": errors}


def fetch_bodies(items: list[dict], limit: int = 30, max_chars: int = 2500) -> None:
    """業界紙RSS等の直リンク記事について本文を取得（LLM入力用・保存しない）。trafilatura が無ければ何もしない。"""
    try:
        import trafilatura
    except ImportError:
        return
    n = 0
    for it in items:
        if n >= limit or "news.google.com" in it["link"]:
            continue
        try:
            raw = trafilatura.fetch_url(it["link"])
            body = trafilatura.extract(raw) if raw else None
            if body:
                it["_body"] = body[:max_chars]
                n += 1
        except Exception:
            pass
