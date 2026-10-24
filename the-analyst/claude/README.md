# Claude から使うためのファイル

このフォルダは GitHub Actions の動作には関係ありません。Claude（claude.ai）からこのツールを使うための控えです。

| ファイル | 用途 |
|---|---|
| `skills/ir-industry-scan/SKILL.md` | Claude のスキル。会話で「〇〇業界の儲かっている事業を調べて」と頼むと、Claude が `requests/<ID>.json` を push → GitHub Actions で実行 → 結果を読んで報告する |
| `artifact/the-analyst.html` | Artifact「The Analyst」のページ（依頼フォーム・依頼の状況・レポート表示） |
| `worker-task-prompt.md` | （任意）Artifact のボタンから依頼を自動実行するための、スケジュールされたタスクの本文 |

## 仕組み

```
Claude（会話 / Artifact） ──push──▶ requests/<ID>.json
                                     │
                         GitHub Actions「IR Scan」（EDINET・ニュースを取得して解析）
                                     │
            requests/<ID>.result.json ＋ docs/reports/<日時-ID>/（index.html, compact.json ほか）をコミット
                                     │
Claude が結果を読み取り、会話で報告／Artifact に表示
```

Claude の作業環境からは EDINET に直接接続できないため、取得と解析は GitHub Actions 上で行います。

## Artifact のデータ構造

Artifact の共有データベースに、次の形で保存します。

| パス | 内容 | 書き込み |
|---|---|---|
| `requests/<ID>` | 依頼（mode, target, …, status: queued / running / done / error, report_id, error, run_url） | ページの利用者 |
| `reports/<slug>` | レポート本体（`docs/reports/<slug>/compact.json` の内容） | オーナー（Claude） |
| `report_index/<slug>` | 一覧用の要約（slug, title, subject, generated_at, companies） | オーナー（Claude） |
| `config/app` | `{"trigger_id": "trig_..."}`（自動実行タスクを使う場合のみ） | オーナー |

Artifact を作り直すときは、`artifact/the-analyst.html` を Claude に渡して「この内容で Artifact を公開して」と依頼してください。データベースの権限は次のとおりです：reports・report_index・config は閲覧者が読み取り、編集者以上が書き込み。requests は利用者（Contributor 以上）が書き込み。ページから自動実行タスクを起動する場合は「Claude Code Remote」コネクタの `fire_trigger` を許可します。
