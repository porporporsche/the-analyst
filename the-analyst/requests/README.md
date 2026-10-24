# 依頼ファイル

このフォルダに `<任意のID>.json` を追加して push すると、GitHub Actions（IR Scan）がその内容で実行され、
結果が `<ID>.result.json`（成功・失敗とも）と `docs/reports/` に自動コミットされます。Claude からの実行に使います。

```json
{"mode": "company", "target": "トヨタ自動車,本田技研工業", "news_keywords": "自動車", "news_days": 90}
```

| キー | 内容 |
|---|---|
| mode | `company` / `industry` / `check` |
| target | 会社名・証券コード（カンマ区切り）または業種名 |
| name_filter | industry 時の社名絞り込み |
| news_keywords | ニュース検索の業界キーワード |
| max_companies / news_days / lookback_days | 数値（省略可） |
