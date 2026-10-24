# IR × 業界ニュース スキャナー

業界や会社名を指定すると、**EDINET（金融庁）の有価証券報告書・半期報告書**から直近の業績とセグメント別の収益性を集計・可視化し、
**Webニュース・業界紙の情報はIR情報とは別枠で**整理したHTMLレポートを作ります。GitHub Actions 上で動きます。

用途: 「この業界ではどの事業・サービスが儲かっていて、どれが儲かっていないか」を把握し、M&A の候補探索や自社の事業戦略検討の材料にする。

## レポートの構成

| セクション | 情報源 | 内容 |
|---|---|---|
| 総括 | IR＋報道 | 儲かっている／儲かっていない事業類型、M&A への示唆、IR数値と報道のギャップ（根拠が IR か報道かをタグ表示） |
| 業績比較 | IR | 売上・利益・利益率・前年比・CAGR・ROE・直近中間期の伸び、成長性×収益性バブルチャート、売上推移 |
| 事業セグメント分析 | IR | 全社のセグメントを「成長率×利益率」で一枚に配置。高収益・成長セグメント／赤字・縮小セグメントの一覧 |
| 企業別詳細 | IR | 5期推移、セグメント別売上と利益率、好調・苦戦事業、重点施策、有報原文の抜粋、EDINET原本へのリンク |
| ニュース・業界紙 | 報道 | 伸びている／苦戦している領域、主要テーマ、M&A・提携・撤退の動き、記事一覧（見出し＋リンクのみ） |

出力: `docs/reports/<日時>-<モード>-<ID>/index.html`（＋ `data.json` 生データ、`report.md` 要約）。一覧は `docs/index.html`。

## セットアップ（初回のみ・約10分）

1. このフォルダを **プライベート**リポジトリとして GitHub に push します。
2. **EDINET API キーを取得**（無料）: EDINET のサイトにある「API利用登録」から登録し、発行された Subscription-Key を控えます。
3. リポジトリの **Settings → Secrets and variables → Actions → New repository secret** に登録:
   - `EDINET_API_KEY`（必須）
   - `ANTHROPIC_API_KEY`（任意。設定すると Claude が有報の記述・ニュースを要約し「総括」を生成します。未設定でも数値分析とニュース一覧は出ます）
   - モデルを変えたい場合は同じ画面の **Variables** タブに `CLAUDE_MODEL` を設定（既定 `claude-sonnet-5-5`。config.yaml より優先）
4. レポートのコミットが「permission denied」で失敗する場合のみ、**Settings → Actions → General → Workflow permissions** を「Read and write permissions」にします（ワークフロー側で `contents: write` を指定済みのため、通常は不要）。
5. **Actions → IR Scan → Run workflow** で mode に `check` を選んで実行し、Summary に ✅ が並ぶことを確認します（APIキー・EDINET・Googleニュース・Claude APIへの接続を診断）。

## 使い方

**Actions → IR Scan → Run workflow** で入力して実行します。

| 入力 | 例 | 説明 |
|---|---|---|
| mode | `company` / `industry` / `check` | 企業を直接指定するか、業種で抽出するか。`check` は接続診断のみ |
| target | `freee, マネーフォワード, 4478` | company: 社名・証券コード（カンマ区切りで複数社＝比較レポート） |
| | `情報・通信業` | industry: 東証33業種名（部分一致）。資本金の大きい順に最大 `max_companies` 社 |
| name_filter | `システム,ソフト` | industry 時に社名で絞り込み（業種が広すぎる場合に） |
| news_keywords | `SaaS,クラウド会計` | ニュース検索に使う業界キーワード。**33業種より細かい業界を見たい場合は、company モードで競合を列挙＋ここで業界名を指定**するのがおすすめ |
| max_companies | `12` | 最大社数（上限30。EDINETへの負荷と実行時間を抑えるため） |
| news_days | `90` | ニュースの対象期間 |
| commit_report | ✓ | レポートを `docs/reports/` にコミットするか |

所要時間の目安: 初回 5〜15分（EDINETの書類一覧を日付ごとに遡って取得するため。API呼び出しは1秒間隔）、2回目以降は書類一覧のキャッシュが効いて数分。
失敗した場合は実行画面の Summary に理由が表示されます（APIキー無効、業種名の誤り等）。

結果は (a) 実行画面の **Summary** に要約表、(b) **Artifacts** から HTML 一式をダウンロード、(c) `docs/reports/` にコミット、の3通りで確認できます。

> **GitHub Pages について**: Settings → Pages で `main` ブランチの `/docs` を公開すればブラウザで直接閲覧できますが、
> Enterprise 以外のプランでは Pages は**誰でも閲覧可能**になります。M&A 検討など機密性のある調査では Pages を使わず、Artifacts かリポジトリ内のファイルで閲覧してください。

### 33業種の名称

水産・農林業 / 鉱業 / 建設業 / 食料品 / 繊維製品 / パルプ・紙 / 化学 / 医薬品 / 石油・石炭製品 / ゴム製品 / ガラス・土石製品 / 鉄鋼 / 非鉄金属 / 金属製品 / 機械 / 電気機器 / 輸送用機器 / 精密機器 / その他製品 / 電気・ガス業 / 陸運業 / 海運業 / 空運業 / 倉庫・運輸関連業 / 情報・通信業 / 卸売業 / 小売業 / 銀行業 / 証券、商品先物取引業 / 保険業 / その他金融業 / 不動産業 / サービス業

## Claude から使う

Claude（claude.ai）に「〇〇業界の儲かっている事業を調べて」のように頼むと、Claude が `requests/<ID>.json` を push し、
このワークフローが実行されて、結果（`requests/<ID>.result.json` と `docs/reports/`）を Claude が読み取って回答します。

- **会話から使う**: `claude/skills/ir-industry-scan/SKILL.md` を Claude のスキルとして登録します。
- **Artifact（Webページ）から使う**: `claude/artifact/the-analyst.html` が依頼フォームとレポート表示のページです。レポートは `docs/reports/<slug>/compact.json` を取り込んで表示します。
- 詳しくは `claude/README.md`、依頼ファイルの書式は `requests/README.md` を参照してください。
- Claude の作業環境からは EDINET に直接接続できないため、実際の取得・解析は GitHub Actions 上で行います。失敗した場合も `<ID>.result.json` に理由が書かれます。

## 業界紙の追加

`config.yaml` の `news.feeds` に RSS を追加すると、タイトル・概要に業界キーワードか対象社名を含む記事だけを取り込みます。
購読中の業界紙は、各媒体の利用規約の範囲で設定してください。記事本文はLLM要約の入力としてのみ一時利用し、レポートには見出しとリンクだけを残します。

## ローカル実行

```bash
pip install -r requirements.txt
python -m scanner.main --demo                     # キー不要・架空データで見た目を確認
export EDINET_API_KEY=...  ANTHROPIC_API_KEY=...
python -m scanner.main --mode company  --target "トヨタ自動車,本田技研工業" --news-keywords "自動車"
python -m scanner.main --mode industry --target "情報・通信業" --name-filter "システム" --max-companies 8
python -m scanner.main --mode check               # 接続診断
python -m pytest -q tests                         # 実データでの解析テスト＋Actions再現テスト（ネットワーク不要）
```

## 動作確認の状況

- **解析ロジック**: 実際の有価証券報告書3件（トヨタ自動車=IFRS、任天堂=日本基準、キヤノン=米国基準。`tests/fixtures/`）で、
  売上・営業利益・純利益・ROE・従業員数・セグメント別の売上と利益が公表値と一致することをテストしています。
  半期報告書（新様式 `InterimDuration`／旧様式 `CurrentYTDDuration`）のコンテキストにも対応しています。
- **GitHub Actions 上の動作**: `tests/test_e2e.py` で、EDINET API・コードリスト・Googleニュースを実データを返す偽サーバーに差し替え、
  ワークフローと同じコマンドを `GITHUB_OUTPUT`／`GITHUB_STEP_SUMMARY` 付きで実行して、レポート生成・Summary出力・
  APIキー無効時のエラー表示・キャッシュ再利用を検証しています。push時は `Test` ワークフローで自動実行されます。
- **実際のEDINET/Googleへの接続**は、ご自身のリポジトリで `check` モードを実行して確認してください（開発環境からは接続できないため未実施）。

## 仕組みと制約

- **IR データ**: EDINET API v2 の書類一覧を新しい日付から遡り（既定400日）、各社の最新の有価証券報告書と、それより新しい半期報告書を取得。XBRL を CSV 化したデータ（type=5）から「主要な経営指標等の推移」（最大5期）、損益計算書、セグメント情報、記述情報（事業の内容・経営方針・MD&A・事業等のリスク）を抽出します。セグメント名は XBRL 本体のラベル（type=1）から日本語名を取得します。
- 初回は書類一覧を数百日分取得するため 5〜15 分程度かかります。2回目以降は Actions キャッシュ（書類一覧のJSONとコードリストのみ）で短縮されます。
- APIキーはログやレポートに出力しません（エラーメッセージ中のキーも伏せ字にしています）。
- 指標は**XBRLの要素ID**（会社固有の拡張要素を含む）と、書類の**DEI（会計基準）**に基づいて判定し、項目名（日本語ラベル）は補助に使っています。
  IFRS移行企業の5期推移に混在する旧基準の列は除外します。銀行・保険など特殊な様式では一部指標が「—」になることがあります。
- 日本基準の会社は「主要な経営指標等の推移」に営業利益がないため、営業利益は損益計算書の直近2期のみ表示します（5期推移は売上・経常利益・純利益）。
- 米国基準の会社（キヤノン等）はXBRLに営業利益・セグメント数値がない場合があり、その場合は税引前利益で代替します。
- **セグメント利益の定義は会社ごとに異なります**（営業利益・事業利益・調整後など）。会社間比較は目安として扱ってください。
- 決算短信（TDnet）は公式の無償APIが無いため対象外です。直近四半期の速報値が必要な場合は拡張が必要です。
- 対象は EDINET に提出している国内上場企業です（非上場・海外企業は対象外）。

## ファイル構成

```
scanner/
  edinet.py       EDINET API: 企業特定・書類検索・ダウンロード（キャッシュ付き）
  xbrl_parse.py   XBRL(CSV)の解析: 5期推移・セグメント・記述情報
  news.py         Googleニュース RSS ＋ 業界紙RSS の収集
  analyze.py      指標計算、Claude API による要約（任意）
  report.py       Plotly によるグラフと HTML/Markdown/JSON 出力
  templates/      レポートの HTML テンプレート
  demo.py         架空データ（--demo）
tests/                        実データのフィクスチャと、解析・結合テスト
.github/workflows/scan.yml   手動実行ワークフロー（company / industry / check）
.github/workflows/test.yml   push時の自動テスト
requests/                    Claude 等からの依頼ファイル（push で IR Scan が起動）
claude/                      Claude から使うためのスキル・Artifactページ・タスク本文
config.yaml                   ニュース・LLM の設定
```
