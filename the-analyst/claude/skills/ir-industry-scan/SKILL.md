---
name: ir-industry-scan
description: 業界や企業のIR情報（EDINET有報）とニュースから、儲かっている/儲かっていない事業を調べるとき（M&A・事業戦略検討）。the-analystリポジトリのGitHub Actionsで実行する。
---

# IR × 業界ニュース スキャン（the-analyst）

ユーザーが「〇〇業界でどの事業が儲かっているか」「A社とB社の業績・セグメントを比較して」「M&A候補として△△を調べて」のように頼んだら、このスキルで GitHub リポジトリ `porporporsche/the-analyst` のツールを実行し、結果を会話で報告する。

**重要**: Claude の作業環境からは EDINET（api.edinet-fsa.go.jp）や Google ニュースに直接接続できない。取得と解析は必ず GitHub Actions 上で行う。依頼ファイルを push すると、ワークフロー `IR Scan` が起動する。

## 1. 依頼内容を決める

| ユーザーの言い方 | mode | target |
|---|---|---|
| 会社名・証券コードを挙げた（複数可） | `company` | `"トヨタ自動車,本田技研工業"` または `"7203,7267"` |
| 東証33業種で広く見たい（例：情報・通信業、小売業） | `industry` | 業種名。社名で絞るときは `name_filter`（例 `"システム,ソフト"`） |
| 33業種より細かい業界（例：SaaS、クラウド会計、介護） | `company` | 代表的な上場企業を Claude が3〜8社選んで列挙し、`news_keywords` に業界名を入れる。どの会社を選んだかはユーザーに伝える |
| 接続確認・初回 | `check` | 省略可 |

- `news_keywords`: ニュース検索の業界キーワード（カンマ区切り）。
- 任意: `max_companies`（既定12、上限30）、`news_days`（既定90）。
- 依頼が曖昧で社数や範囲が大きく変わる場合だけ、1回だけ質問する。それ以外はすぐ実行する。

## 2. リポジトリを用意する

1. `add_repo`（owner `porporporsche`, repo `the-analyst`, access `push`）でセッションに追加する。push が拒否される旨が返ったら、Claude の GitHub アプリを the-analyst にインストールするよう案内して止める（https://github.com/apps/claude/installations/select_target）。
2. `/home/claude/the-analyst` が無ければ `git clone --depth 1 https://github.com/porporporsche/the-analyst /home/claude/the-analyst`。あれば `git -C /home/claude/the-analyst pull --rebase origin main`。

## 3. 依頼ファイルを push する

```bash
cd /home/claude/the-analyst
RID=$(TZ=Asia/Tokyo date +%Y%m%d-%H%M%S)-<短い英数字の説明>   # 例: 20261017-2105-saas
cat > requests/$RID.json <<'EOF'
{"mode": "company", "target": "freee,マネーフォワード", "news_keywords": "クラウド会計", "news_days": 90}
EOF
git add requests/$RID.json
git -c user.name=Claude -c user.email=noreply@anthropic.com commit -m "request: $RID" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
git pull --rebase -q origin main && git push origin main
SHA=$(git rev-parse HEAD)
```

- API キーなどの秘密情報を依頼ファイルやコミットに書かない（キーは GitHub Secrets にある）。
- push したら「GitHub で分析を開始した。初回は5〜15分、2回目以降は数分」と一言伝える。

## 4. 完了を待つ

ワークフローは完了すると `requests/<RID>.result.json` をコミットする（成功・失敗とも）。

```bash
A=https://api.github.com/repos/porporporsche/the-analyst
until curl -sS "$A/actions/runs?head_sha=$SHA" | python3 -c "import json,sys; rs=[r for r in json.load(sys.stdin)['workflow_runs'] if r['name']=='IR Scan']; sys.exit(0 if rs and rs[0]['status']=='completed' else 1)"; do sleep 15; done
git pull -q --rebase origin main
cat requests/$RID.result.json
```

- 1回のコマンドで待つのは最大10分程度にし、長引く場合は繰り返す。25分を超えたら run_url を伝えて、後で確認する。

## 5. 結果を読んで報告する

`result.json` の `status`:

- **error**: `error` と `check`（check モード時）を分かる言葉で伝える。
  - `EDINET_API_KEY が設定されていません` / `APIキーが無効` → Settings → Secrets and variables → Actions で `EDINET_API_KEY` を登録・更新してもらう。
  - 業種名が無い → エラーに列挙された業種名から選び直して再実行を提案。
  - 企業が見つからない → 証券コード4桁での指定を提案。
  - 原因不明なら `run_url` を案内する。
- **ok**: `report_dir`（例 `docs/reports/20261017-2110-company-ab12cd`）の中を読む。
  - `report.md`: 企業比較表と高収益セグメントの要約
  - `compact.json` / `data.json`: 数値（companies[].metrics の revenue, profit, margin, yoy, cagr, roe, segments[]）、highlights、synthesis・news_digest（ANTHROPIC_API_KEY 設定時）、news.items、warnings
  - `index.html`: グラフ付きレポート

報告は次の順に簡潔に（数値は data.json の値をそのまま使い、推測で補わない）:

1. **結論**（2〜3文）：儲かっている事業／儲かっていない事業
2. **IR情報（有価証券報告書）**：企業比較の小さな表、高収益で伸びているセグメント、赤字・縮小セグメント
3. **報道（ニュース・業界紙）**：IR とは分けて、伸びている／苦戦している領域や M&A の動きをリンク付きで
4. **M&A・事業戦略への示唆**（IR と報道のどちらに基づくかを明記）
5. 注意点：warnings、セグメント利益の定義が会社ごとに異なること、日本基準の会社は営業利益が直近2期のみであること

Artifact「The Analyst」を使っている場合は、`compact.json` をその Artifact の `reports/<slug>` に、一覧用の要約を `report_index/<slug>` に書き込むと、ページにも表示される（claude/README.md 参照）。

## 6. 注意

- リポジトリの `docs/reports/` に過去のレポートが溜まる。同じ対象の最近のレポートがあれば、再利用するか新しく実行するかを確認してよい。
- ツール自体の改修を頼まれた場合は、通常のコード修正として the-analyst を編集し、`python -m pytest -q tests` を通してから push する。コミットは `user.name=Claude`, `user.email=noreply@anthropic.com` で行う。
