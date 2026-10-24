"""EDINET API v2 クライアント: 企業の特定、提出書類の検索、書類ZIPのダウンロード。

実レスポンスで確認した仕様:
- 認証エラー: {"StatusCode":401,"message":"Access denied due to invalid subscription key."}
- 書類なし等: {"metadata":{"status":"404","message":"Not Found",...},"results":null}（HTTP 200 のことがある）
- 書類一覧(type=2)の各行: docID, edinetCode, secCode, docTypeCode, periodEnd, submitDateTime,
  withdrawalStatus("0"=通常), csvFlag("1"=CSVあり), xbrlFlag, pdfFlag ...
- 書類取得の成功時は ZIP（先頭が "PK"）、失敗時は JSON
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
import unicodedata
import zipfile
from pathlib import Path

import requests

API = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
DOC_VIEW_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"
JST = dt.timezone(dt.timedelta(hours=9))

DOC_ANNUAL = "120"   # 有価証券報告書
DOC_INTERIM = "160"  # 半期報告書（2024年4月以降、上場企業の四半期報告書は廃止され半期報告書に）
DOC_QUARTER = "140"  # 四半期報告書（2024年3月以前）

# EDINETコードリスト(CSV, cp932)の列位置
_CL = {"edinet_code": 0, "filer_type": 1, "listed": 2, "consolidated": 3, "capital": 4, "fiscal_end": 5,
       "name": 6, "name_en": 7, "name_kana": 8, "address": 9, "industry": 10, "sec_code": 11}


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def norm(s: str) -> str:
    """社名照合用の正規化（全半角・空白・法人格を除去）。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"(株式会社|\(株\)|有限会社|合同会社|ホールディングス|グループ本社|\s)", "", s)
    return s.lower()


class EdinetError(RuntimeError):
    pass


class EdinetAuthError(EdinetError):
    pass


class EdinetClient:
    def __init__(self, api_key: str | None, cache_dir: str | Path = ".cache/edinet", sleep: float = 1.0, log=print):
        if not api_key:
            raise EdinetAuthError(
                "EDINET_API_KEY が設定されていません。EDINETのAPI利用登録で取得したキーを "
                "リポジトリの Settings → Secrets and variables → Actions に EDINET_API_KEY として登録してください。"
            )
        self.api_key = api_key.strip()
        self.cache = Path(cache_dir)
        (self.cache / "docs").mkdir(parents=True, exist_ok=True)
        self.files = self.cache.parent / "edinet-files"  # 大きいZIPはActionsキャッシュ対象外の場所に置く
        self.files.mkdir(parents=True, exist_ok=True)
        self.sleep = sleep  # 金融庁の「節度ある利用」に合わせ、API呼び出しの間隔を空ける
        self.log = log
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "ir-industry-scanner/1.1"
        self._last = 0.0
        self.calls = 0

    # ------------------------------------------------------------------ HTTP
    def _redact(self, text: str) -> str:
        text = str(text).replace(self.api_key, "***")
        return re.sub(r"(Subscription-Key=)[^&\s'\")]+", r"\1***", text)

    def _get(self, url: str, params: dict | None = None, auth: bool = True) -> requests.Response:
        params = dict(params or {})
        if auth:
            params["Subscription-Key"] = self.api_key
        last = ""
        for attempt in range(5):
            wait = self.sleep - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                r = self.s.get(url, params=params, timeout=120, allow_redirects=False)
            except requests.RequestException as e:
                last = self._redact(f"{type(e).__name__}: {e}")
                time.sleep(min(10 * (2 ** attempt), 120))
                continue
            if r.status_code == 401 or _json_status(r) == "401":
                raise EdinetAuthError("EDINET APIキーが無効です（401）。Secrets の EDINET_API_KEY を確認してください。")
            if r.status_code == 429:
                last = "HTTP 429（アクセス過多）"
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(max(30 * (2 ** attempt), int(ra) if ra.isdigit() else 0), 300))
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}（EDINET側のエラー・メンテナンスの可能性）"
                time.sleep(min(15 * (2 ** attempt), 120))
                continue
            if r.status_code != 200:
                raise EdinetError(f"EDINET HTTP {r.status_code}: {self._redact(r.text[:200])}")
            return r
        raise EdinetError(f"EDINETへの接続に失敗しました（再試行上限）: {last}")

    # ------------------------------------------------------------ code list
    def load_codelist(self) -> list[dict]:
        p = self.cache / "Edinetcode.zip"
        if not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400:
            r = self._get(CODELIST_URL, auth=False)
            if r.content[:2] != b"PK":
                raise EdinetError("EDINETコードリストのZIPを取得できませんでした")
            p.write_bytes(r.content)
        return parse_codelist(p.read_bytes())

    def resolve_companies(self, queries: list[str]) -> tuple[list[dict], list[str]]:
        """社名 / 証券コード(4桁) / EDINETコードから上場企業を特定する。"""
        rows = self.load_codelist()
        listed = [r for r in rows if r["listed"] == "上場"]
        for r in listed:
            r["_n"] = norm(r["name"])
        found, warnings = [], []
        for q in [q.strip() for q in queries if q.strip()]:
            qu = unicodedata.normalize("NFKC", q).upper()
            hit: list[dict] = []
            if re.fullmatch(r"E\d{5}", qu):
                hit = [r for r in rows if r["edinet_code"] == qu]
            elif re.fullmatch(r"\d[0-9A-Z]\d[0-9A-Z]0?", qu):
                hit = [r for r in listed if r["sec_code"][:4] == qu[:4]]
            if not hit:
                qn = norm(q)
                hit = [r for r in listed if r["_n"] == qn] or sorted(
                    [r for r in listed if qn and qn in r["_n"]], key=lambda r: len(r["_n"]))
                if len(hit) > 1:
                    warnings.append(f"「{q}」は複数候補に一致: {', '.join(r['name'] for r in hit[:5])} → 「{hit[0]['name']}」を採用"
                                    "（別の会社の場合は証券コードで指定してください）")
            if not hit:
                warnings.append(f"「{q}」に一致する上場企業が見つかりませんでした（証券コード4桁でも指定できます）")
                continue
            found.append(_company(hit[0]))
        uniq = {c["edinet_code"]: c for c in found}
        return list(uniq.values()), warnings

    def companies_by_industry(self, industry: str, max_n: int = 12, name_filter: str = "") -> list[dict]:
        """EDINETの提出者業種（≒東証33業種）で上場企業を抽出し、資本金の大きい順に max_n 社。"""
        rows = [r for r in self.load_codelist() if r["listed"] == "上場"]
        key = unicodedata.normalize("NFKC", industry).strip()
        hit = [r for r in rows if key and key in unicodedata.normalize("NFKC", r["industry"])]
        if name_filter:
            pats = [norm(x) for x in re.split(r"[,、\s]+", name_filter) if x]
            hit = [r for r in hit if any(p in norm(r["name"]) for p in pats)]
        if not hit:
            inds = sorted({r["industry"] for r in rows} - {""})
            raise EdinetError(f"業種「{industry}」に一致する企業がありません。指定可能な業種: {' / '.join(inds)}")
        hit.sort(key=lambda r: -_num(r["capital"]))
        return [_company(r) for r in hit[:max_n]]

    # ------------------------------------------------------------ documents
    def documents_on(self, day: dt.date) -> list[dict]:
        p = self.cache / "docs" / f"{day.isoformat()}.json"
        settled = day < today_jst() - dt.timedelta(days=7)  # 直近は追加提出・取下げがあり得るので再取得
        if p.exists() and settled:
            return json.loads(p.read_text("utf-8"))
        r = self._get(f"{API}/documents.json", {"date": day.isoformat(), "type": 2})
        try:
            data = r.json()
        except ValueError:
            raise EdinetError(f"書類一覧({day})の応答がJSONではありません")
        status = _json_status(r)
        if status != "200":
            msg = (data.get("metadata") or {}).get("message") or data.get("message")
            raise EdinetError(f"EDINET書類一覧APIエラー({day}, status={status}): {msg}")
        results = data.get("results") or []
        p.write_text(json.dumps(results, ensure_ascii=False), "utf-8")
        return results

    def find_latest_filings(self, edinet_codes: list[str], lookback_days: int = 400) -> dict[str, dict]:
        """各社の最新の有価証券報告書と、それより新しい半期報告書を、今日から日付を遡って探す。"""
        want = set(edinet_codes)
        found: dict[str, dict] = {c: {} for c in want}
        today = today_jst()
        for i in range(lookback_days):
            day = today - dt.timedelta(days=i)
            if day.weekday() >= 5:
                continue
            if i and i % 30 == 0:
                done = sum(DOC_ANNUAL in v for v in found.values())
                self.log(f"  … {day} まで遡り済み（有報 {done}/{len(want)} 社 取得済み）")
            for d in self.documents_on(day):
                c = d.get("edinetCode")
                if c not in want or str(d.get("withdrawalStatus", "0")) != "0" or str(d.get("csvFlag")) != "1":
                    continue
                t = str(d.get("docTypeCode"))
                if t == DOC_ANNUAL and DOC_ANNUAL not in found[c]:
                    found[c][DOC_ANNUAL] = d
                elif t in (DOC_INTERIM, DOC_QUARTER) and DOC_ANNUAL not in found[c] and "interim" not in found[c]:
                    found[c]["interim"] = d
            if all(DOC_ANNUAL in v for v in found.values()):
                break
        return found

    def download(self, doc_id: str, kind: int) -> bytes:
        """kind=5: XBRLをCSV化したZIP, kind=1: 提出本文書(XBRL)のZIP"""
        if not re.fullmatch(r"S[0-9A-Z]{7}", doc_id or ""):
            raise EdinetError(f"docID の形式が不正です: {doc_id!r}")
        p = self.files / f"{doc_id}_{kind}.zip"
        if p.exists():
            return p.read_bytes()
        r = self._get(f"{API}/documents/{doc_id}", {"type": kind})
        if r.content[:2] != b"PK":
            raise EdinetError(f"書類 {doc_id}(type={kind}) を取得できません: status={_json_status(r)}")
        p.write_bytes(r.content)
        return r.content


def _json_status(r: requests.Response) -> str | None:
    if r.content[:2] == b"PK":
        return "200"
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if "StatusCode" in body:
        return str(body["StatusCode"])
    md = body.get("metadata")
    return str(md.get("status")) if isinstance(md, dict) and md.get("status") is not None else None


def parse_codelist(blob: bytes) -> list[dict]:
    """EDINETコードリスト(ZIP内CSV, cp932)を列位置で読む。先頭の説明行・見出し行は自動で読み飛ばす。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("cp932", errors="replace")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 12 or not re.fullmatch(r"E\d{5}", row[0].strip()):
            continue
        rows.append({k: row[i].strip() for k, i in _CL.items()})
    if not rows:
        raise EdinetError("EDINETコードリストの形式を読めませんでした")
    return rows


def _num(s: str) -> float:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return 0.0


def _company(r: dict) -> dict:
    return {
        "edinet_code": r["edinet_code"], "sec_code": (r.get("sec_code") or "")[:4], "name": r["name"],
        "name_en": r.get("name_en", ""), "industry": r.get("industry", ""), "fiscal_end": r.get("fiscal_end", ""),
    }
