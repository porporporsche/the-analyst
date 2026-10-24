"""GitHub Actions 上の実行を再現する結合テスト。

EDINET API・コードリスト・Googleニュースへの通信を、実データのフィクスチャを返す偽サーバーに差し替え、
ワークフローと同じコマンド（python -m scanner.main ...）を GITHUB_OUTPUT / GITHUB_STEP_SUMMARY 付きで実行する。
"""
import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from scanner import edinet as edinet_mod
from scanner import main as M

FX = Path(__file__).parent / "fixtures"
KEY = "test-key-123456"


def _codelist() -> bytes:
    header = ["ＥＤＩＮＥＴコード", "提出者種別", "上場区分", "連結の有無", "資本金", "決算日", "提出者名", "提出者名（英字）",
              "提出者名（ヨミ）", "所在地", "提出者業種", "証券コード", "提出者法人番号"]
    rows = [["ダウンロード実行日", "2026年10月15日現在", "件数", "4件"], header,
            ["E02144", "内国法人・組合", "上場", "有", "635401", "3月31日", "トヨタ自動車株式会社", "TOYOTA", "", "", "輸送用機器", "72030", ""],
            ["E02367", "内国法人・組合", "上場", "有", "10065", "3月31日", "任天堂株式会社", "Nintendo", "", "", "その他製品", "79740", ""],
            ["E02274", "内国法人・組合", "上場", "有", "174762", "12月31日", "キヤノン株式会社", "CANON", "", "", "電気機器", "77510", ""],
            ["E99999", "内国法人・組合", "非上場", "有", "100", "3月31日", "トヨタ非上場株式会社", "", "", "", "輸送用機器", "", ""]]
    text = "\r\n".join(",".join(f'"{c}"' for c in r) for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("EdinetcodeDlInfo.csv", text.encode("cp932"))
    return buf.getvalue()


def _lab_zip(members: dict[str, str]) -> bytes:
    locs = "".join(
        f'<link:loc xlink:type="locator" xlink:href="x.xsd#jpcrp030000-asr_E02144-000_{m}" xlink:label="{m}"/>'
        f'<link:label xlink:type="resource" xlink:label="label_{m}" xlink:role="http://www.xbrl.org/2003/role/label" xml:lang="ja">{ja}</link:label>'
        f'<link:labelArc xlink:type="arc" xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label" xlink:from="{m}" xlink:to="label_{m}"/>'
        for m, ja in members.items())
    xml = ('<?xml version="1.0" encoding="UTF-8"?><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
           'xmlns:xlink="http://www.w3.org/1999/xlink"><link:labelLink xlink:type="extended" '
           f'xlink:role="http://www.xbrl.org/2003/role/link">{locs}</link:labelLink></link:linkbase>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_2024-03-31_01_2024-06-25_lab.xml", xml)
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_2024-03-31_01_2024-06-25_lab-en.xml", xml.replace("自動車", "Automotive"))
    return buf.getvalue()


RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Google News</title>
<item><title>トヨタ、EV戦略を見直し - 日本経済新聞</title><link>https://news.google.com/rss/articles/a1</link>
<pubDate>{d1}</pubDate><source url="https://www.nikkei.com">日本経済新聞</source></item>
<item><title>任天堂の新型機、販売好調 - ITmedia</title><link>https://news.google.com/rss/articles/a2</link>
<pubDate>{d2}</pubDate><source url="https://www.itmedia.co.jp">ITmedia</source></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}
        self.text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeWeb:
    """EDINET / Googleニュースの偽サーバー。"""

    def __init__(self, today: dt.date, key_ok=True):
        self.calls: list[str] = []
        self.key_ok = key_ok
        lists = {n: json.loads((FX / n).read_text("utf-8"))["results"] for n in
                 ("list_2024-06-25.json", "list_2024-06-28.json", "list_2024-03-28.json")}
        # 実データの書類一覧を「最近の日付」に置き直す（有報は6月提出、キヤノンは3月）
        self.by_day = {
            _weekday(today - dt.timedelta(days=110)): lists["list_2024-06-25.json"],
            _weekday(today - dt.timedelta(days=107)): lists["list_2024-06-28.json"],
            _weekday(today - dt.timedelta(days=200)): lists["list_2024-03-28.json"],
        }
        self.docs = {
            "S100TR7I": (FX / "csv_toyota_ifrs_S100TR7I.zip").read_bytes(),
            "S100TMMG": (FX / "csv_nintendo_jpgaap_S100TMMG.zip").read_bytes(),
            "S100T58N": (FX / "csv_canon_usgaap_S100T58N.zip").read_bytes(),
        }
        self.lab = _lab_zip({"AutomotiveReportableSegmentMember": "自動車", "FinancialServicesReportableSegmentMember": "金融"})

    def get(self, url, params=None, **kw):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        q.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append(url)
        if "Subscription-Key" in q and (not self.key_ok or q["Subscription-Key"] != KEY):
            return FakeResponse(401, (FX / "error_invalid_key.json").read_bytes())
        if url.endswith("/codelist/Edinetcode.zip"):
            return FakeResponse(200, _codelist(), {"Content-Type": "application/octet-stream"})
        if url.endswith("/documents.json"):
            day = dt.date.fromisoformat(q["date"])
            res = self.by_day.get(day, [])
            body = {"metadata": {"status": "200", "message": "OK", "resultset": {"count": len(res)}}, "results": res}
            return FakeResponse(200, json.dumps(body).encode())
        m = re.search(r"/documents/(S[0-9A-Z]{7})$", url)
        if m:
            doc, kind = m.group(1), q.get("type")
            if doc in self.docs and kind == "5":
                return FakeResponse(200, self.docs[doc], {"Content-Type": "application/octet-stream"})
            if doc == "S100TR7I" and kind == "1":
                return FakeResponse(200, self.lab, {"Content-Type": "application/octet-stream"})
            if doc in self.docs and kind == "1":
                return FakeResponse(200, _lab_zip({}))
            return FakeResponse(200, (FX / "error_doc_not_found_404.json").read_bytes())
        if "news.google.com/rss" in url:
            now = dt.datetime.now(dt.timezone.utc)
            fmt = "%a, %d %b %Y %H:%M:%S GMT"
            body = RSS.format(d1=(now - dt.timedelta(days=2)).strftime(fmt), d2=(now - dt.timedelta(days=5)).strftime(fmt))
            return FakeResponse(200, body.encode())
        return FakeResponse(404, b"not found")


def _weekday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


@pytest.fixture()
def gh_env(tmp_path, monkeypatch):
    """GitHub Actions のランナーと同じ環境変数を用意する。"""
    out, summ = tmp_path / "gh_output", tmp_path / "gh_summary"
    out.touch(), summ.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    monkeypatch.setenv("EDINET_SLEEP", "0")
    monkeypatch.setenv("SCANNER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_MODEL", "")  # vars.CLAUDE_MODEL 未設定時は空文字が渡る
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(M, "ROOT", tmp_path)
    monkeypatch.setattr("time.sleep", lambda s: None)
    web = FakeWeb(edinet_mod.today_jst())
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: web.get(url, **kw))
    monkeypatch.setattr(requests, "get", lambda url, **kw: web.get(url, **kw))
    return {"out": out, "summary": summ, "root": tmp_path, "web": web}


def test_company_mode_like_github(gh_env):
    rc = M.main(["--mode", "company", "--target", "トヨタ自動車, 7974, キヤノン", "--name-filter", "",
                 "--news-keywords", "", "--max-companies", "12", "--news-days", "90",
                 "--config", str(Path(__file__).parents[1] / "config.yaml")])
    assert rc == 0
    outputs = dict(line.split("=", 1) for line in gh_env["out"].read_text().splitlines())
    report_dir = gh_env["root"] / outputs["report_dir"]
    assert outputs["report_dir"].startswith("docs/reports/") and (report_dir / "index.html").exists()
    html = (report_dir / "index.html").read_text("utf-8")
    # 3社の業績・トヨタのセグメント（日本語名）・ニュースが入っている
    for s in ("トヨタ自動車株式会社", "任天堂株式会社", "キヤノン株式会社", "自動車", "金融", "450,953", "16,719",
              "トヨタ、EV戦略を見直し", "WZEK0040.aspx?S100TR7I"):
        assert s in html, s
    data = json.loads((report_dir / "data.json").read_text("utf-8"))
    toyota = next(c for c in data["companies"] if c["edinet_code"] == "E02144")
    assert [x["segment"] for x in toyota["metrics"]["segments"]] == ["自動車", "金融", "その他"]
    assert (gh_env["root"] / "docs" / "index.html").exists()
    summary = gh_env["summary"].read_text("utf-8")
    assert "| トヨタ自動車株式会社 |" in summary and "自動車" in summary
    # APIキーがレポートやログ用の出力に含まれない
    assert KEY not in html and KEY not in summary and KEY not in json.dumps(data, ensure_ascii=False)


def test_industry_mode(gh_env):
    rc = M.main(["--mode", "industry", "--target", "輸送用機器", "--max-companies", "5", "--news-days", "30"])
    assert rc == 0
    outputs = dict(line.split("=", 1) for line in gh_env["out"].read_text().splitlines())
    html = (gh_env["root"] / outputs["report_dir"] / "index.html").read_text("utf-8")
    assert "トヨタ自動車株式会社" in html and "トヨタ非上場" not in html


def test_unknown_industry_fails_with_message(gh_env):
    rc = M.main(["--mode", "industry", "--target", "宇宙旅行業"])
    assert rc == 1
    s = gh_env["summary"].read_text("utf-8")
    assert "実行に失敗しました" in s and "輸送用機器" in s  # 指定可能な業種を案内


def test_invalid_key_fails_clearly(gh_env, monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", "wrong-key")
    rc = M.main(["--mode", "company", "--target", "トヨタ自動車"])
    assert rc == 1
    s = gh_env["summary"].read_text("utf-8")
    assert "APIキーが無効" in s and "wrong-key" not in s


def test_missing_key(gh_env, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY")
    assert M.main(["--mode", "company", "--target", "トヨタ自動車"]) == 1
    assert "EDINET_API_KEY が設定されていません" in gh_env["summary"].read_text("utf-8")


def test_check_mode(gh_env, monkeypatch):
    assert M.main(["--mode", "check", "--target", "任天堂"]) == 0
    s = gh_env["summary"].read_text("utf-8")
    assert "接続診断" in s and "EDINET API" in s and "任天堂株式会社(7974)" in s and "Googleニュース" in s
    monkeypatch.setenv("EDINET_API_KEY", "wrong-key")
    assert M.main(["--mode", "check"]) == 1


def test_second_run_uses_cache(gh_env):
    args = ["--mode", "company", "--target", "任天堂", "--news-days", "7"]
    assert M.main(args) == 0
    first = len([u for u in gh_env["web"].calls if "documents.json" in u])
    gh_env["web"].calls.clear()
    assert M.main(args) == 0
    second = len([u for u in gh_env["web"].calls if "documents.json" in u])
    assert second < first / 5  # 確定済みの日付は再取得しない


class _FakeAnthropic:
    """Claude API の代わり。プロンプトの種類に応じたJSONを返す。"""

    def __init__(self, **kw):
        self.models = []
        outer = self

        class _Messages:
            def create(self, model, max_tokens, system, messages):
                outer.models.append(model)
                p = messages[0]["content"]
                if "headline" in p:
                    d = {"headline": "自動車が稼ぎ頭", "profitable": [{"business": "自動車", "why": "高利益率", "examples": "トヨタ", "source": "IR"}],
                         "unprofitable": [], "ir_vs_news": ["特になし"], "ma_implications": ["金融事業の強化"]}
                elif "overview" in p:
                    d = {"overview": "EV見直しの報道が目立つ", "themes": [], "growing": [], "declining": [], "deals": []}
                else:
                    d = {"summary": "会社要約テキスト", "profitable": [], "struggling": [], "strategy": [], "risks": [], "ma_view": "x"}
                import types
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="```json\n" + json.dumps(d, ensure_ascii=False) + "\n```")])

        self.messages = _Messages()


def test_llm_path_and_model_precedence(gh_env, monkeypatch):
    import anthropic
    fake = _FakeAnthropic()
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")  # Variables の CLAUDE_MODEL が config.yaml より優先
    assert M.main(["--mode", "company", "--target", "トヨタ自動車", "--news-days", "7",
                   "--config", str(Path(__file__).parents[1] / "config.yaml")]) == 0
    assert fake.models and set(fake.models) == {"claude-opus-5-5"}
    outputs = dict(line.split("=", 1) for line in gh_env["out"].read_text().splitlines())
    html = (gh_env["root"] / outputs["report_dir"] / "index.html").read_text("utf-8")
    assert "自動車が稼ぎ頭" in html and "EV見直しの報道が目立つ" in html and "会社要約テキスト" in html


def _request(root, rid, body):
    d = root / "requests"
    d.mkdir(exist_ok=True)
    p = d / f"{rid}.json"
    p.write_text(json.dumps(body, ensure_ascii=False), "utf-8")
    return p


def test_request_file_success(gh_env):
    p = _request(gh_env["root"], "20261017-2040-toyota", {"mode": "company", "target": "トヨタ自動車", "news_days": 30})
    assert M.main(["--request", str(p)]) == 0
    res = json.loads(p.with_name("20261017-2040-toyota.result.json").read_text("utf-8"))
    assert res["status"] == "ok" and res["error"] is None and res["companies"] == ["トヨタ自動車株式会社"]
    meta = json.loads((gh_env["root"] / res["report_dir"] / "meta.json").read_text("utf-8"))
    assert meta["request_id"] == "20261017-2040-toyota"
    assert (gh_env["root"] / res["report_dir"] / "report.md").exists()


def test_request_file_failure_writes_reason(gh_env):
    p = _request(gh_env["root"], "bad", {"mode": "industry", "target": "宇宙旅行業"})
    assert M.main(["--request", str(p)]) == 1
    res = json.loads(p.with_name("bad.result.json").read_text("utf-8"))
    assert res["status"] == "error" and "輸送用機器" in res["error"]


def test_request_file_check(gh_env):
    p = _request(gh_env["root"], "chk", {"mode": "check", "target": "任天堂"})
    assert M.main(["--request", str(p)]) == 0
    res = json.loads(p.with_name("chk.result.json").read_text("utf-8"))
    assert res["status"] == "ok" and any(r["item"].startswith("EDINET API") and r["ok"] for r in res["check"])


def test_request_file_invalid(gh_env):
    p = _request(gh_env["root"], "broken", {"mode": "delete-everything"})
    assert M.main(["--request", str(p)]) == 1
    assert json.loads(p.with_name("broken.result.json").read_text("utf-8"))["status"] == "error"


def test_compact_json_for_artifact(gh_env):
    assert M.main(["--mode", "company", "--target", "トヨタ自動車,任天堂,キヤノン", "--news-days", "30"]) == 0
    outputs = dict(line.split("=", 1) for line in gh_env["out"].read_text().splitlines())
    raw = (gh_env["root"] / outputs["report_dir"] / "compact.json").read_bytes()
    assert len(raw) < 200_000  # Artifact の db 文書上限（256KiB）未満
    c = json.loads(raw)
    toyota = next(x for x in c["companies"] if x["name"] == "トヨタ自動車株式会社")
    assert [s["segment"] for s in toyota["segments"]] == ["自動車", "金融", "その他"]
    assert round(toyota["revenue"] / 1e8) == 450953 and toyota["doc_url"].endswith("S100TR7I")
