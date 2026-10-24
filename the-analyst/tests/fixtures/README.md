# テスト用フィクスチャ（EDINET API v2 の実レスポンス）

| ファイル | 内容 |
|---|---|
| `csv_toyota_ifrs_S100TR7I.zip` | トヨタ自動車 有価証券報告書（2024年3月期・IFRS）の type=5（XBRL→CSV） |
| `csv_nintendo_jpgaap_S100TMMG.zip` | 任天堂 有価証券報告書（2024年3月期・日本基準）の type=5 |
| `csv_canon_usgaap_S100T58N.zip` | キヤノン 有価証券報告書（2023年12月期・米国基準）の type=5 |
| `list_*.json` | 書類一覧API（documents.json?type=2）の応答（抜粋） |
| `error_*.json` | エラー応答（無効なキー、書類なし） |

出典: [hermes-plugin-edinet](https://github.com/TakeshiTGAL/hermes-plugin-edinet)（MIT License, Copyright (c) 2026 TakeshiTGAL）の tests/fixtures。
データの出典：EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100TR7I 、S100TMMG 、S100T58N）、
公共データ利用規約 PDL1.0（https://www.digital.go.jp/resources/open_data/public_data_license_v1.0）
