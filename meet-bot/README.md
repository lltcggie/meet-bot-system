# meet-bot

Google Meet の録画・文字起こし生成イベントを受け取り、議事録を Slack にきれいに整形して投稿する Webhook サーバーです。会議名のプレフィックス (`[Project]` など) に応じて Drive ファイルの整理と投稿先チャンネルの振り分けも行います。

## 主な機能

- `files_generated` Webhook を受け取り、Google Docs の議事録本文を Slack の Block Kit に変換して投稿
- 議事録中のタイムスタンプ `(HH:MM:SS)` を Drive 動画の該当秒に飛ぶリンクに置換
- 議事録中の `(一部の録画は利用できません)` プレースホルダーを `recording_ids` の動画リンクに差し替え
- 議事録中の参加者メールアドレスを Slack ユーザー ID に解決してメンション化
- Gemini が付与する評価導線・注意書きなどのノイズ行を除去
- 会議名のプレフィックス `[xxx]` に応じて Google Drive の保存先フォルダと Slack 投稿先チャンネルを切り替え
- プレフィックスが無い場合 (または Drive 移動失敗時) は `organizer_email` に対して Bot から DM
- Slack のメッセージ Block 数制限 (50) を超える場合はスレッド + チャンネル再掲 (`reply_broadcast`) で分割投稿
- Slack ソケットモードで動くスラッシュコマンド `/meetbot` によりプレフィックス設定を管理者が動的に更新可能

## 必要なもの

### Google Workspace 側

- サービスアカウント (ドメイン全体委任 / Domain-Wide Delegation 有効)
- サービスアカウントに次のスコープを付与
  - `https://www.googleapis.com/auth/drive`
  - `https://www.googleapis.com/auth/documents.readonly`
  - `https://www.googleapis.com/auth/userinfo.email`
- 会議主催者 (`organizer_email`) が所属するドメインで、上記サービスアカウントの成りすましが許可されていること

### Slack 側

- Slack アプリを 1 つ作成 (Socket Mode を有効化)
- Bot Token Scopes
  - `chat:write`
  - `users:read`
  - `users:read.email`
  - `im:write`
  - `commands`
- App-Level Token (`xapp-...`) を発行し、`connections:write` スコープを付与
- スラッシュコマンド `/meetbot` を登録 (Socket Mode の場合 Request URL は不要)

## 環境変数

| 変数名 | 必須 | 内容 |
| --- | --- | --- |
| `SERVICE_ACCOUNT_JSON` | ○ | サービスアカウント JSON の中身 (ファイルパスではなく本文を入れる) |
| `SLACK_BOT_TOKEN` | ○ | `xoxb-` から始まる Bot User OAuth Token |
| `SLACK_APP_TOKEN` | ○ | `xapp-` から始まる App-Level Token (Socket Mode 用) |
| `WEBHOOK_SHARED_SECRET` | ○ | `/webhook` の HMAC-SHA256 署名検証に使う共有シークレット。未設定の場合は全ての Webhook リクエストを拒否 |
| `PORT` | | Flask サーバーのポート (デフォルト `8080`) |
| `PREFIX_MAPPING_PATH` | | プレフィックス設定の永続化ファイルパス (デフォルト `./data/meetbot_mapping.json`) |

`SERVICE_ACCOUNT_JSON` / `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `WEBHOOK_SHARED_SECRET` は起動直後にプロセス環境から削除されます。

## セットアップ

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install flask google-auth google-api-python-client requests slack-sdk
```

環境変数を設定して起動します (PowerShell 例):

```powershell
$env:SERVICE_ACCOUNT_JSON = Get-Content service_account.json -Raw
$env:SLACK_BOT_TOKEN = "xoxb-..."
$env:SLACK_APP_TOKEN = "xapp-..."
python main.py
```

bash 例:

```bash
export SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
python main.py
```

起動時に `PREFIX_MAPPING_PATH` が存在すればロードします。Socket Mode は別スレッドで常駐します。

## Docker

イメージをビルド:

```bash
docker build -t meet-bot .
```

データ永続化のために `/app/data` をボリュームマウントしてください。

```bash
docker run -d \
  --name meet-bot \
  -e SERVICE_ACCOUNT_JSON="$(cat service_account.json)" \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_APP_TOKEN="xapp-..." \
  -v $(pwd)/data:/app/data \
  -p 8080:8080 \
  meet-bot
```

`PREFIX_MAPPING_PATH` のデフォルトは `./data/meetbot_mapping.json` なので、`data` ディレクトリをマウントすればプレフィックス設定がコンテナ再作成後も維持されます。書き込みは一時ファイル + `os.replace` による原子的差し替えなので、不意の中断で破損しづらい設計です。

内部では `gunicorn` が `workers=1, threads=4` で動きます。Socket Mode の接続は worker プロセス内で常駐するため、複数 worker にしてしまうと同じ App Token で複数接続が張られ、スラッシュコマンドの書き込みが競合し得るので worker=1 に固定しています。

コンテナは非 root ユーザー `app` (uid=1000, gid=1000) で実行されます。ホストの `./data` ディレクトリをマウントする場合、そのディレクトリが uid=1000 で書き込み可能である必要があります。例:

```bash
mkdir -p data
sudo chown 1000:1000 data
```

または、ホストの自分の UID で動かしたい場合は `--user` で上書きできます。

```bash
docker run --user "$(id -u):$(id -g)" ...
```

## エンドポイント

### `POST /webhook`

録画・文字起こし生成イベントを受け取ります。以下のヘッダによる HMAC-SHA256 署名検証が必須で、検証に失敗したリクエストは 401 を返します。

- `X-Webhook-Timestamp`: Unix epoch 秒
- `X-Webhook-Signature`: `sha256=<hex digest>` 形式 (プレフィックス `sha256=` は省略可)

署名対象は `"<timestamp>." + <raw body bytes>`、鍵は `WEBHOOK_SHARED_SECRET`。タイムスタンプが現在時刻から 5 分以上ずれているリクエストはリプレイ対策として拒否します。

期待する JSON:

```json
{
  "event": "files_generated",
  "meeting_code": "wpi-xbcv-soe",
  "space_name": "spaces/xxxxx",
  "organizer_email": "user@example.com",
  "conference_record": {
    "name": "conferenceRecords/xxx",
    "start_time": "2026/04/28 10:00:00.000000",
    "end_time": "2026/04/28 11:00:00.000000",
    "expire_time": "...",
    "space": "spaces/xxxxx"
  },
  "recording_ids": ["<Drive file id>"],
  "transcript_ids": ["<Drive file id>"]
}
```

`event != "files_generated"` や `transcript_ids` 空のイベントはそのまま 200 で無視します。

### `GET /health`

ヘルスチェック用。常に `{"status":"ok"}` を返します。

## 処理フロー

1. `transcript_ids` が空なら終了
2. `organizer_email` を subject にしたサービスアカウントの委任資格情報を生成
3. `recording_ids` / `transcript_ids` のファイル名から会議名を抽出
   - 録画名: `<会議名> - YYYY/MM/DD HH:mm JST〜Recording`
   - 録画名がデフォルト (例: `wpi-xbcv-soe (2026-03-18 02:27 GMT+9)`) の場合はミーティングコードを会議名とする
   - recording が無ければ transcript 名: `<会議名> - YYYY/MM/DD HH:mm JST - Gemini によるメモ`
   - transcript もデフォルト (`... に開始した会議 - Gemini によるメモ`) の場合は「会議」を会議名とする
4. Docs API で transcript を取得
5. Slack の Block Kit に変換
   - 見出し (`HEADING_1/2/TITLE`): `header` block + 📌 絵文字
   - 中見出し以下 (`HEADING_3/4/5/6/SUBTITLE`): `header` block
   - 段落内 bold は小見出しとして独立行に
   - `(HH:MM:SS)` や段落内リンク付き `HH:MM:SS` を Drive 動画タイムスタンプ URL に置換
   - `(一部の録画は利用できません)` を `recording_ids` の「録画」リンクに差し替え
   - メールアドレスを Slack ユーザー ID に解決してメンション化
   - Gemini の評価フレーズ・フッタを除外
6. 会議名冒頭の `[xxx]` プレフィックスを抽出し、設定されていれば:
   - Drive 上で `recording_ids + transcript_ids` を指定フォルダに移動 (失敗時はプレフィックスなし扱い)
   - 投稿先 Slack チャンネルを指定チャンネルにする
7. プレフィックスが無い / マッピング未登録 / 移動失敗 の場合は `organizer_email` に対して Bot から DM
8. Block 数が 50 を超える場合は分割し、2 通目以降は 1 通目のスレッドに `reply_broadcast=true` で投稿

## スラッシュコマンド `/meetbot`

ワークスペースの管理者 (Admin / Owner / Primary Owner) のみが実行可能です。ソケットモード経由で受け取り、Slack API で `users.info` を呼んで権限を確認します。

| サブコマンド | 機能 |
| --- | --- |
| `/meetbot list` | 登録されているプレフィックス一覧 |
| `/meetbot set <プレフィックス> <DriveフォルダID> <Slackチャンネル>` | 追加 / 更新 |
| `/meetbot remove <プレフィックス>` | 削除 |
| `/meetbot help` | 使い方を表示 |

プレフィックスは会議名冒頭の角括弧込みの形式で指定します (例: `[Ariadne]`)。`Slackチャンネル` はチャンネル ID (`C01234567`) か `#channel-name` どちらでも可。空白を含む場合はダブルクォートで囲んでください。

設定は即座に `PREFIX_MAPPING_PATH` に原子的書き込みで保存され、次回起動時に読み込まれます。

## テスト用リクエスト (PowerShell)

```powershell
$body = @{
    event = "files_generated"
    meeting_code = "wpi-xbcv-soe"
    space_name = "spaces/xxxxx"
    organizer_email = "you@example.com"
    conference_record = @{
        name = "conferenceRecords/abc123"
        start_time = "2026/04/28 10:00:00.000000"
        end_time = "2026/04/28 11:00:00.000000"
        expire_time = "2026/05/28 10:00:00.000000"
        space = "spaces/xxxxx"
    }
    recording_ids = @("<Drive file id>")
    transcript_ids = @("<Drive file id>")
} | ConvertTo-Json -Depth 5

$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$ts = [string][int][double]::Parse((Get-Date -UFormat %s))
$secretBytes = [System.Text.Encoding]::UTF8.GetBytes($env:WEBHOOK_SHARED_SECRET)
$signBase = [System.Text.Encoding]::UTF8.GetBytes("$ts.") + $bodyBytes
$hmac = New-Object System.Security.Cryptography.HMACSHA256
$hmac.Key = $secretBytes
$sig = ($hmac.ComputeHash($signBase) | ForEach-Object { $_.ToString("x2") }) -join ""

Invoke-RestMethod -Uri "http://localhost:8080/webhook" -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Headers @{
        "X-Webhook-Timestamp" = $ts
        "X-Webhook-Signature" = "sha256=$sig"
    } `
    -Body $bodyBytes
```

## 制限事項・既知の挙動

- Slack Connect 経由の外部ユーザーは `users.lookupByEmail` がヒットする場合のみメンションに置換されます。ヒットしない場合は `名前` テキスト表記のままです。
- Slack の `header` block は plain_text のみのため、見出しの文字装飾やリンクは適用されません。
- Slack メッセージあたりの section block は mrkdwn 3000 文字制限があり、約 2900 文字でチャンク分割しています。
- Drive ファイル移動 (`files.update`) は共有ドライブを含むため `supportsAllDrives=True` で呼び出しています。Drive API 呼び出しは会議主催者 (`organizer_email`) に成りすました資格情報で行われるため、主催者が移動元ファイルおよび移動先フォルダに対する書き込み権限を持っている必要があります。
