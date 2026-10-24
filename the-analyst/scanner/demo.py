"""APIキー無しでレポートの見た目を確認するための架空データ。実在企業とは無関係。"""
from __future__ import annotations

import datetime as dt

OKU = 1e8


def _series(revs, profits, nets):
    n = len(revs)
    return {
        "revenue": {n - 1 - i: v * OKU for i, v in enumerate(revs)},
        "operating_income": {n - 1 - i: v * OKU for i, v in enumerate(profits)},
        "net_income": {n - 1 - i: v * OKU for i, v in enumerate(nets)},
    }


def _seg(name, rev, rev_p, prof, prof_p):
    return {"segment": name, "member": name, "revenue": rev * OKU, "revenue_prior": rev_p * OKU,
            "profit": prof * OKU, "profit_prior": prof_p * OKU}


def demo_companies() -> list[dict]:
    spec = [
        ("E99001", "9901", "架空クラウド株式会社", [420, 480, 560, 650, 760], [38, 52, 70, 91, 118], [25, 34, 47, 60, 80],
         [_seg("会計SaaS", 410, 340, 98, 72), _seg("人事労務SaaS", 230, 190, 24, 15), _seg("導入コンサル", 120, 120, -4, 4)],
         {"business": "当社グループは中小企業向けクラウド会計・人事労務サービスを提供しています。（デモ用の架空テキスト）"}),
        ("E99002", "9902", "サンプルシステムズ株式会社", [1800, 1850, 1900, 1880, 1920], [140, 150, 152, 141, 138], [90, 96, 99, 90, 88],
         [_seg("受託開発", 1100, 1120, 66, 80), _seg("保守運用", 560, 520, 78, 70), _seg("ハードウェア販売", 260, 240, -6, -9)], {}),
        ("E99003", "9903", "テストデータ株式会社", [210, 260, 330, 420, 520], [-12, -5, 8, 30, 52], [-15, -8, 4, 19, 35],
         [_seg("データ分析基盤", 380, 300, 60, 34), _seg("広告ソリューション", 140, 120, -8, -4)], {}),
        ("E99004", "9904", "モデルネットワークス株式会社", [960, 940, 910, 900, 870], [70, 58, 41, 30, 18], [44, 35, 22, 14, 6],
         [_seg("通信回線", 520, 545, 30, 38), _seg("データセンター", 230, 205, 31, 24), _seg("端末販売", 120, 150, -43, -32)], {}),
        ("E99005", "9905", "ダミーセキュリティ株式会社", [150, 175, 205, 245, 300], [22, 27, 33, 41, 54], [14, 18, 22, 28, 37],
         [_seg("マネージドセキュリティ", 300, 245, 54, 41)], {}),
    ]
    out = []
    for code, sec, name, revs, profs, nets, segs, texts in spec:
        out.append({
            "edinet_code": code, "sec_code": sec, "name": name, "industry": "情報・通信業",
            "annual": {"basis": "連結", "standard": "日本基準", "series": _series(revs, profs, nets),
                       "period_end": "2026-03-31", "submit_date": "2026-06-20", "doc_id": None},
            "interim": {"series": {"revenue": {0: revs[-1] * 0.52 * OKU, 1: revs[-2] * 0.5 * OKU},
                                   "operating_income": {0: profs[-1] * 0.5 * OKU, 1: profs[-2] * 0.5 * OKU}},
                        "period_end": "2026-09-30", "doc_id": None, "doc_label": "半期報告書"},
            "segments": segs, "texts": texts, "errors": [],
        })
    return out


def demo_news() -> dict:
    today = dt.date.today()
    titles = [
        ("中小企業のクラウド会計導入が加速、インボイス対応が追い風", "架空IT新聞"),
        ("受託開発大手、人材不足で採算悪化 単価交渉が課題に", "サンプル経済"),
        ("データセンター需要が拡大 生成AI向け投資相次ぐ", "デモ日報"),
        ("通信端末販売の縮小続く、各社が事業再編を検討", "架空IT新聞"),
        ("セキュリティ運用サービスの受注が好調、M&Aで規模拡大", "テスト通信"),
        ("広告ソリューション市場、Cookie規制で再編へ", "サンプル経済"),
    ]
    items = [{"id": f"n{i:08d}", "title": t, "source": s, "link": "https://example.com/",
              "date": (today - dt.timedelta(days=3 * i + 1)).isoformat(), "snippet": "", "query": "デモ",
              "channel": "Webニュース"} for i, (t, s) in enumerate(titles)]
    return {"items": items, "queries": ["（デモ）"], "feeds": [], "errors": []}
