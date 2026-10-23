# YouTube 日本ランキング（通常動画のみ・完全無料）

日本で人気の YouTube **通常動画**（ショート除外）の

- **急上昇**: 投稿72時間以内の動画を、直近4時間の再生ペース（新しさで補正）で順位付け
- **デイリー**: 直近24時間の再生数の増加量順
- **ウィークリー**: 直近7日間の再生数の増加量順

を2時間ごとに自動更新して公開するサイトです。

## 費用が0円の理由

| 部分 | 使うもの | 費用 |
|---|---|---|
| データ取得 | YouTube Data API v3 の無料枠（1日10,000ユニット） | 0円（クレジットカード登録不要） |
| 定期実行 | GitHub Actions（公開リポジトリは無制限） | 0円 |
| 公開 | GitHub Pages | 0円 |

このスクリプトは1回あたり最大750ユニットまでしか使わないように制限しています（1日12回で最大9,000ユニット）。上限を超えても課金はされず、その日はAPIが止まるだけです。

## セットアップ（10分くらい）

### 1. YouTube APIキーを取る
1. https://console.cloud.google.com/ を開いて新しいプロジェクトを作る
2. 「APIとサービス」→「ライブラリ」で **YouTube Data API v3** を有効にする
3. 「認証情報」→「認証情報を作成」→「APIキー」
4. 作ったキーの「APIの制限」で YouTube Data API v3 だけを許可（推奨）

※ 請求先アカウント（クレカ）の登録は不要です。

### 2. GitHubに公開リポジトリを作ってアップロード
このフォルダの中身をそのまま push します（Pages を無料で使うため **Public** にしてください）。

```
git init && git add . && git commit -m "init"
git branch -M main
git remote add origin https://github.com/<あなたのID>/yt-jp-ranking.git
git push -u origin main
```

### 3. 設定
1. リポジトリの **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `YT_API_KEY` / Secret: 手順1のキー
2. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にする
3. **Actions タブ → update-ranking → Run workflow** で1回目を手動実行

数分後に `https://<あなたのID>.github.io/yt-jp-ranking/` で見られます。以降は2時間ごとに自動で更新されます。

## 最初の数日について
- 再生数の推移はこのツールが自分で記録しています。そのため、デイリーが正確になるのは追跡を始めて24時間後、ウィークリーは7日後からです。それまでの増加量は推定値（画面に「推定」と表示）です。
- 追跡するチャンネルは、使うほど増えていきます。人気の動画が見つかるとそのチャンネルを覚えて、以降は新着を毎回チェックします。

## 仕組み
1. 候補集め
   - 公式の `mostPopular`（日本）：音楽・映画・ゲーム
   - 検索：直近48時間で再生数の多い日本語の4分以上の動画（1回100ユニットなので2回だけ）
   - 覚えたチャンネルの新着：勢いのある順に125件＋残りを巡回で125件
2. 選別
   - 日本語の動画か：音声言語・タイトルのかな・チャンネルの国で判定
   - ショートを除外：60秒以下は除外。61〜180秒は `youtube.com/shorts/ID` に問い合わせて判定（クォータを使わない）
   - 配信中・配信予定のライブは除外
3. 追跡中の全動画の再生数を記録して、伸びからランキングを計算 → `site/data/ranking.json`

## 調整したいとき
`collector/collect.py` の先頭にある `CFG`：

- `trend_max_age_h`: 急上昇の対象にする投稿の新しさ（初期値72時間）
- `trend_window_h`: 急上昇の速度を測る時間（初期値4時間）
- `channel_poll_per_run`: 1回で新着をチェックするチャンネル数（1件あたり1ユニット）
- `quota_budget_per_run`: 1回で使うユニットの上限

## ローカルでテスト（APIキーなし）
```
YT_MOCK=1 python3 collector/collect.py
cd site && python3 -m http.server
```
`tools/mock_api.py` の架空データで動きます。

## 注意
- YouTube公式のランキングではありません（ページ下部にも表記しています）。
- YouTube API サービス利用規約に従い、取得したデータは8日以内に古いものから削除しています。
