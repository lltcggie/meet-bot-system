# Google Meet イベント監視

Google Workspace Events + Google Meet REST API を使って、組織内ユーザーの主催会議に関するイベントを監視します。
録画と文字起こしが両方完了したタイミングで、`on_all_file_generated()` が別スレッドで呼ばれます。

また、会議終了時および録画・文字起こしが全て生成された時に、設定した URL へ Webhook として POST します。

## 必要条件

- Google Workspace 管理者権限
- ドメイン全体の委任が有効なサービスアカウント
- Admin SDK / Workspace Events / Meet API / Pub/Sub を有効化済み

## 環境変数

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `SERVICE_ACCOUNT_JSON` | Y | サービスアカウント JSON を 1 行文字列化したもの |
| `ADMIN_USER_EMAIL` | Y | ドメイン全体の委任ユーザー（管理者） |
| `PROJECT_ID` | Y | Google Cloud プロジェクト ID |
| `PUBSUB_TOPIC_ID` | Y | Pub/Sub トピック ID |
| `PUBSUB_SUBSCRIPTION_ID` | Y | Pub/Sub サブスクリプション ID |
| `SUBSCRIPTION_TTL` |  | サブスクリプション TTL（例: `86400s`） |
| `RECREATE_SUBSCRIPTION` |  | `true` のとき既存サブスクリプションを削除して作成し直す |
| `WEBHOOK_URL` |  | Webhook の送信先 URL。未設定時は送信をスキップ |
| `WEBHOOK_TIMEOUT` |  | Webhook 送信時のタイムアウト秒数（デフォルト: `30`） |
| `WEBHOOK_SHARED_SECRET` | Y(Webhook 使用時) | HMAC-SHA256 署名に使用する共有シークレット。未設定時は送信をスキップ |

## service_account.json を環境変数文字列にする（PowerShell）

```
(Get-Content .\service_account.json -Raw | ConvertTo-Json -Compress | ConvertFrom-Json | ConvertTo-Json -Compress)
```

## 実行方法（ローカル）

```
python main.py
```

## 実行方法（Docker）

```
docker build -t meet-events .
```

```
docker run --rm \
	-e SERVICE_ACCOUNT_JSON="..." \
	-e ADMIN_USER_EMAIL="admin@example.com" \
	-e PROJECT_ID="your-project-id" \
	-e PUBSUB_TOPIC_ID="meet-events-topic" \
	-e PUBSUB_SUBSCRIPTION_ID="meet-events-sub" \
	-e SUBSCRIPTION_TTL="86400s" \
	-e RECREATE_SUBSCRIPTION="false" \
	-e WEBHOOK_URL="https://example.com/webhook" \
	-e WEBHOOK_TIMEOUT="30" \
	-e WEBHOOK_SHARED_SECRET="your-shared-secret" \
	meet-events
```

## Webhook 仕様

`WEBHOOK_URL` を設定すると、以下のタイミングで JSON を POST します。

- 会議終了時（`event`: `conference_ended`）
- 録画・文字起こしが全て生成完了したとき（`event`: `files_generated`）

### ヘッダ

- `Content-Type: application/json; charset=utf-8`
- `X-Webhook-Timestamp: <unix_epoch_seconds>` — 送信時刻（Unix epoch 秒）。受信側でリプレイ攻撃対策として現在時刻との差分をチェックしてください（例: 5分以内）。
- `X-Webhook-Signature: sha256=<hex_digest>` — HMAC-SHA256 署名。署名対象は `"<timestamp>." + <raw_body_bytes>`、鍵は `WEBHOOK_SHARED_SECRET`。受信側で `hmac.compare_digest` 等の定数時間比較で検証してください。

### 受信側での検証例（Python）

```python
import hmac, hashlib, time

def verify(request_body_bytes, timestamp_header, signature_header, secret):
    # タイムスタンプの鮮度チェック（5分以内）
    if abs(int(time.time()) - int(timestamp_header)) > 300:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp_header}.".encode("utf-8") + request_body_bytes,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)
```

### ボディ

共通フィールド:

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `event` | string | `conference_ended` または `files_generated` |
| `meeting_code` | string | 会議コード |
| `space_name` | string | スペース名（例: `spaces/xxx`） |
| `organizer_email` | string | 主催者のメールアドレス |
| `conference_record` | object | ConferenceRecord の情報（`name` / `start_time` / `end_time` / `expire_time` / `space`） |
| `recording_ids` | string[] | 録画ファイルの Drive ファイル ID 一覧（`files_generated` のみ） |
| `transcript_ids` | string[] | 文字起こしの Docs ドキュメント ID 一覧（`files_generated` のみ） |

### 例（`files_generated`）

```json
{
  "event": "files_generated",
  "meeting_code": "abc-defg-hij",
  "space_name": "spaces/xxxxxxx",
  "organizer_email": "user@example.com",
  "conference_record": {
    "name": "conferenceRecords/xxxxxxx",
    "start_time": "2026/04/28 10:00:00.000000",
    "end_time": "2026/04/28 11:00:00.000000",
    "expire_time": "2026/05/28 11:00:00.000000",
    "space": "spaces/xxxxxxx"
  },
  "recording_ids": ["drive-file-id-1"],
  "transcript_ids": ["docs-document-id-1"]
}
```

## 重要

- `RECREATE_SUBSCRIPTION=true` の場合、各ユーザーの既存サブスクリプションを削除します。
- 監視中に `Ctrl+C` で終了できます。
