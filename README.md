# meet-bot-system

Google Meet の録画・文字起こし生成イベントを監視し、議事録を Slack に投稿する 2 サービス構成のアプリケーションです。

- [meet-webhook](meet-webhook/README.md) — Workspace Events + Meet API で会議終了・ファイル生成イベントを検知し、署名付き Webhook として送信
- [meet-bot](meet-bot/README.md) — 受け取った Webhook をもとに議事録を整形して Slack に投稿 / Drive ファイルをフォルダへ振り分け

## Docker Compose での起動

### 1. `.env` を用意する

[.env.example](.env.example) を `.env` にコピーして値を埋めてください。

```bash
cp .env.example .env
```

`SERVICE_ACCOUNT_JSON` は改行を含まない 1 行の JSON 文字列にする必要があります (PowerShell 例):

```powershell
(Get-Content .\service_account.json -Raw | ConvertFrom-Json | ConvertTo-Json -Compress)
```

`WEBHOOK_SHARED_SECRET` は meet-webhook が署名し meet-bot が検証する HMAC-SHA256 共有鍵です。両サービス間で同一の値を使ってください。

### 2. ビルドして起動する

```bash
docker compose up -d --build
```

起動順は `depends_on` + `healthcheck` で制御されており、meet-bot の `GET /health` が成功してから meet-webhook が起動します。meet-webhook は `WEBHOOK_URL=http://meet-bot:8080/webhook` でコンテナ間通信します。

### 3. ログを確認する

```bash
docker compose logs -f meet-webhook
docker compose logs -f meet-bot
```

### 4. 停止する

```bash
docker compose down
```

## 永続化

meet-bot のプレフィックス設定ファイル (`/app/data/meetbot_mapping.json`) はホストの [data/](data/) にマウントされているため、コンテナを作り直してもスラッシュコマンドで登録した設定は維持されます。

コンテナは非 root (uid=1000) で動くため、`data/` の書き込み権限が必要です。Linux で権限エラーが出る場合:

```bash
sudo chown -R 1000:1000 data
```

## 環境変数一覧

詳細は各サービスの README を参照してください。

| 変数 | 用途 | 使用サービス |
| --- | --- | --- |
| `SERVICE_ACCOUNT_JSON` | サービスアカウント JSON (1 行) | 両方 |
| `WEBHOOK_SHARED_SECRET` | Webhook 署名の共有鍵 | 両方 |
| `ADMIN_USER_EMAIL` | ドメイン全体委任の管理者メール | meet-webhook |
| `PROJECT_ID` | GCP プロジェクト ID | meet-webhook |
| `PUBSUB_TOPIC_ID` | Pub/Sub トピック ID | meet-webhook |
| `PUBSUB_SUBSCRIPTION_ID` | Pub/Sub サブスクリプション ID | meet-webhook |
| `SUBSCRIPTION_TTL` | Workspace サブスクリプション TTL | meet-webhook |
| `RECREATE_SUBSCRIPTION` | 既存サブスクリプションの再作成フラグ | meet-webhook |
| `WEBHOOK_URL` | Webhook 送信先 (既定: `http://meet-bot:8080/webhook`) | meet-webhook |
| `WEBHOOK_TIMEOUT` | Webhook 送信タイムアウト秒 | meet-webhook |
| `SLACK_BOT_TOKEN` | `xoxb-` で始まる Bot Token | meet-bot |
| `SLACK_APP_TOKEN` | `xapp-` で始まる App-Level Token | meet-bot |
