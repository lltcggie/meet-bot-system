import hashlib
import hmac
import json
import os
import sys
import signal
import logging
import threading
import time
import requests
from collections import deque
from datetime import datetime, timedelta, timezone

from google.auth.transport import requests as google_requests
from google.oauth2 import service_account
from google.oauth2.service_account import Credentials
from google.cloud import pubsub_v1
from google.apps import meet_v2 as meet
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    'https://www.googleapis.com/auth/pubsub',
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

ADMIN_USER_EMAIL = os.environ.get("ADMIN_USER_EMAIL", "")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
PROJECT_ID = os.environ.get("PROJECT_ID", "")
PUBSUB_TOPIC_ID = os.environ.get("PUBSUB_TOPIC_ID", "")
PUBSUB_SUBSCRIPTION_ID = os.environ.get("PUBSUB_SUBSCRIPTION_ID", "")
SUBSCRIPTION_TTL = os.environ.get("SUBSCRIPTION_TTL", "86400s")
RECREATE_SUBSCRIPTION = os.environ.get("RECREATE_SUBSCRIPTION", "false")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_TIMEOUT = os.environ.get("WEBHOOK_TIMEOUT", "30")
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")

_ENV_KEYS_TO_CLEAR = [
    "ADMIN_USER_EMAIL",
    "SERVICE_ACCOUNT_JSON",
    "PROJECT_ID",
    "PUBSUB_TOPIC_ID",
    "PUBSUB_SUBSCRIPTION_ID",
    "SUBSCRIPTION_TTL",
    "RECREATE_SUBSCRIPTION",
    "WEBHOOK_URL",
    "WEBHOOK_TIMEOUT",
    "WEBHOOK_SHARED_SECRET",
]
for _key in _ENV_KEYS_TO_CLEAR:
    os.environ.pop(_key, None)

TOPIC_NAME = f"projects/{PROJECT_ID}/topics/{PUBSUB_TOPIC_ID}"
SUBSCRIPTION_NAME = f"projects/{PROJECT_ID}/subscriptions/{PUBSUB_SUBSCRIPTION_ID}"

WORKSPACE_SUBSCRIPTION_NAME: str | None = None
SEEN_EVENT_IDS: deque[str] = deque(maxlen=5000)
SEEN_EVENT_ID_SET: set[str] = set()
ENDED_CONFERENCE_RECORDS: set[str] = set()
RECORDINGS_READY_FOR: set[str] = set()
TRANSCRIPTS_READY_FOR: set[str] = set()
RECORDING_FILE_IDS: dict[str, list[str]] = {}
TRANSCRIPT_FILE_IDS: dict[str, list[str]] = {}
USER_EMAIL_CACHE: dict[str, str] = {}
KNOWN_ORG_USER_IDS: set[str] = set()

EVENT_TYPES = [
    "google.workspace.meet.conference.v2.ended",
    "google.workspace.meet.recording.v2.fileGenerated",
    "google.workspace.meet.transcript.v2.fileGenerated",
]


def load_credentials() -> tuple[Credentials, Credentials]:
    """Load service account credentials and apply domain-wide delegation."""
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            f"サービスアカウントの認証情報が環境変数SERVICE_ACCOUNT_JSONに設定されていません"
        )

    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SERVICE_ACCOUNT_JSON の形式が不正です。") from exc
    base_credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=SCOPES,
    )
        
    delegated_credentials = (
        base_credentials.with_subject(ADMIN_USER_EMAIL)
        if ADMIN_USER_EMAIL
        else base_credentials
    )
    return delegated_credentials, base_credentials


USER_CREDENTIALS, BASE_CREDENTIALS = load_credentials()
PUBSUB_CREDENTIALS = BASE_CREDENTIALS


def get_user_id_for_email(expected_email: str) -> str:
    """Get the Cloud Identity user ID for the signed-in user and verify email."""
    service = build(
        "people",
        "v1",
        credentials=USER_CREDENTIALS,
        cache_discovery=False,
    )
    response = (
        service.people()
        .get(resourceName="people/me", personFields="emailAddresses")
        .execute()
    )

    emails = [
        e.get("value", "").lower()
        for e in response.get("emailAddresses", [])
        if e.get("value")
    ]
    if expected_email.lower() not in emails:
        raise RuntimeError(
            "認証したアカウントのメールが一致しません。"
            f" 期待値: {expected_email} / 実際: {emails}"
        )

    resource_name = response.get("resourceName", "")
    if not resource_name.startswith("people/"):
        raise RuntimeError("People API の resourceName が不正です。")

    return resource_name[len("people/") :]


def subscribe_to_user(
    user_id: str,
    topic_name: str,
    credentials: Credentials,
):
    """Subscribe to all Meet events for a user."""
    session = google_requests.AuthorizedSession(credentials)
    body = {
        "targetResource": f"//cloudidentity.googleapis.com/users/{user_id}",
        "eventTypes": EVENT_TYPES,
        "payloadOptions": {"includeResource": False},
        "notificationEndpoint": {"pubsubTopic": topic_name},
        "ttl": SUBSCRIPTION_TTL,
    }
    return session.post("https://workspaceevents.googleapis.com/v1/subscriptions", json=body)


def get_existing_subscription_name(response) -> str | None:
    """Extract existing subscription name from a 409 response if available."""
    try:
        payload = response.json()
    except ValueError:
        return None

    details = payload.get("error", {}).get("details", [])
    for detail in details:
        if detail.get("reason") == "SUBSCRIPTION_ALREADY_EXISTS":
            metadata = detail.get("metadata", {})
            return metadata.get("current_subscription")
    return None


def get_created_subscription_name(response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload.get("name")


def should_recreate_subscription() -> bool:
    return RECREATE_SUBSCRIPTION.strip().lower() in {"1", "true", "yes", "on"}

def get_subscription_name_from_event(payload: dict) -> str | None:
    subscription = payload.get("subscription") or {}
    return subscription.get("name")


def get_user_id_from_event(payload: dict, event_subject: str) -> str | None:
    """lifecycleイベントの対象ユーザーIDを、event_subjectまたはpayloadから抽出する。"""
    prefix = "//cloudidentity.googleapis.com/users/"
    if event_subject.startswith(prefix):
        return extract_validated_user_id(event_subject)

    subscription = payload.get("subscription") or {}
    target_resource = subscription.get("targetResource") or ""
    if target_resource.startswith(prefix):
        user_id = target_resource[len(prefix):]
        if user_id and "/" not in user_id and is_valid_org_user_id(user_id):
            return user_id
    return None


def get_delegated_credentials_for_user(user_email: str) -> Credentials:
    if not user_email:
        return USER_CREDENTIALS
    return BASE_CREDENTIALS.with_subject(user_email)


def get_user_email_by_id(user_id: str) -> str | None:
    if not user_id:
        return None
    cached = USER_EMAIL_CACHE.get(user_id)
    if cached is not None:
        return cached
    service = build(
        "admin",
        "directory_v1",
        credentials=USER_CREDENTIALS,
        cache_discovery=False,
    )
    try:
        user = service.users().get(userKey=user_id).execute()
    except Exception as exc:
        logger.error("ユーザー取得失敗: %s", exc)
        return None
    email = user.get("primaryEmail")
    if email:
        USER_EMAIL_CACHE[user_id] = email
    return email


def get_credentials_for_user_id(user_id: str):
    user_email = get_user_email_by_id(user_id)
    return get_delegated_credentials_for_user(user_email or "")


def get_event_credentials(event_subject: str) -> Credentials | None:
    if event_subject.startswith("//cloudidentity.googleapis.com/users/"):
        user_id = extract_validated_user_id(event_subject)
        if not user_id:
            return None
        return get_credentials_for_user_id(user_id)
    return USER_CREDENTIALS


def get_event_user_email(event_subject: str) -> str:
    if event_subject.startswith("//cloudidentity.googleapis.com/users/"):
        user_id = extract_validated_user_id(event_subject)
        if not user_id:
            return ""
        return get_user_email_by_id(user_id) or ""
    return ""


def list_org_user_ids() -> list[tuple[str, str]]:
    service = build(
        "admin",
        "directory_v1",
        credentials=USER_CREDENTIALS,
        cache_discovery=False,
    )
    users: list[tuple[str, str]] = []
    page_token = None
    while True:
        response = (
            service.users()
            .list(
                customer="my_customer",
                maxResults=500,
                orderBy="email",
                pageToken=page_token,
            )
            .execute()
        )
        for user in response.get("users", []):
            if user.get("suspended"):
                continue
            user_id = user.get("id")
            if not user_id:
                continue
            users.append((user_id, user.get("primaryEmail", "")))
            KNOWN_ORG_USER_IDS.add(user_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return users


def is_valid_org_user_id(user_id: str) -> bool:
    """user_id が組織内のユーザーかを検証する。キャッシュに無ければ Admin API で再検証する。"""
    if not user_id or not user_id.isdigit():
        return False
    if user_id in KNOWN_ORG_USER_IDS:
        return True
    email = get_user_email_by_id(user_id)
    if email:
        KNOWN_ORG_USER_IDS.add(user_id)
        return True
    return False


def extract_validated_user_id(event_subject: str) -> str | None:
    """event_subject から user_id を抽出し、組織内ユーザーとして検証する。"""
    prefix = "//cloudidentity.googleapis.com/users/"
    if not event_subject.startswith(prefix):
        return None
    user_id = event_subject[len(prefix):]
    if "/" in user_id or not user_id:
        logger.warning("event_subject の形式が不正: %s", event_subject)
        return None
    if not is_valid_org_user_id(user_id):
        logger.warning("未知または不正な user_id のためスキップ: %s", user_id)
        return None
    return user_id


def renew_workspace_subscription(subscription_name: str, credentials: Credentials) -> bool:
    session = google_requests.AuthorizedSession(credentials)
    url = f"https://workspaceevents.googleapis.com/v1/{subscription_name}?updateMask=ttl"
    body = {"ttl": SUBSCRIPTION_TTL}
    response = session.patch(url, json=body)
    if response.status_code == 200:
        logger.info("サブスクリプション更新: %s", subscription_name)
        return True
    logger.error("サブスクリプション更新失敗: %s", response.status_code)
    logger.error(response.text)
    return False


def delete_workspace_subscription(subscription_name: str, credentials: Credentials) -> bool:
    session = google_requests.AuthorizedSession(credentials)
    url = f"https://workspaceevents.googleapis.com/v1/{subscription_name}"
    response = session.delete(url)
    if response.status_code == 200:
        logger.debug("サブスクリプション削除: %s", subscription_name)
        return True
    logger.error("サブスクリプション削除失敗: %s", response.status_code)
    logger.error(response.text)
    return False


def ensure_subscription_for_user(user_id: str, user_email: str = "") -> bool:
    credentials = get_delegated_credentials_for_user(user_email)
    response = subscribe_to_user(
        user_id=user_id,
        topic_name=TOPIC_NAME,
        credentials=credentials,
    )

    if response.status_code == 200:
        name = None
        try:
            name = response.json().get("response", {}).get('name')
        except ValueError:
            pass
        logger.info("サブスクリプションを作成(%s): %s", user_email, name or "不明")
        return True

    if response.status_code == 409:
        existing_name = get_existing_subscription_name(response)
        if existing_name and should_recreate_subscription():
            logger.debug("既存サブスクリプションを削除開始(%s): %s", user_email, existing_name)
            if delete_workspace_subscription(existing_name, credentials=credentials):
                logger.debug("サブスクリプションを再作成開始(%s): %s", user_email, existing_name)
                response = subscribe_to_user(
                    user_id=user_id,
                    topic_name=TOPIC_NAME,
                    credentials=credentials,
                )
                if response.status_code == 200:
                    logger.info("サブスクリプションを再作成(%s): %s", user_email, existing_name)
                    return True
                logger.error("サブスクリプション再作成失敗(%s): %s", user_email, response.status_code)
                logger.error(response.text)
                return False
            else:
                logger.warning("サブスクリプションの削除に失敗したので既存サブスクリプションを使用します(%s): %s", user_email, existing_name)
        logger.info("既存サブスクリプションを使用(%s): %s", user_email, existing_name or "不明")
        return True

    logger.error("サブスクリプション作成失敗(%s): %s", user_email, response.status_code)
    logger.error(response.text)
    return False


def handle_subscription_lifecycle(event_type: str, payload: dict, event_subject: str) -> None:
    global WORKSPACE_SUBSCRIPTION_NAME

    subscription_name = (
        get_subscription_name_from_event(payload)
        or WORKSPACE_SUBSCRIPTION_NAME
    )
    if not subscription_name:
        logger.warning(
            "イベントペイロードにサブスクリプション名が見つかりません (type=%s, subject=%s, payload=%s)",
            event_type,
            event_subject,
            payload,
        )
        return

    user_id = get_user_id_from_event(payload, event_subject)
    if not user_id:
        logger.error(
            "lifecycleイベントから対象ユーザーを特定できません (type=%s, subject=%s, subscription=%s, payload=%s)",
            event_type,
            event_subject,
            subscription_name,
            payload,
        )
        return

    credentials = get_credentials_for_user_id(user_id)

    if event_type.endswith("expirationReminder"):
        renew_workspace_subscription(subscription_name, credentials=credentials)
        return

    if event_type.endswith("expired"):
        response = subscribe_to_user(
            user_id=user_id,
            topic_name=TOPIC_NAME,
            credentials=credentials,
        )
        if response.status_code == 200:
            new_name = get_created_subscription_name(response)
            if new_name:
                WORKSPACE_SUBSCRIPTION_NAME = new_name
            logger.info("期限切れ後にサブスクリプションを再作成しました")
        else:
            logger.error("再サブスクライブ失敗: %s", response.status_code)
            logger.error(response.text)


def get_conference_record_from_child(resource_name: str) -> str | None:
    if not resource_name:
        return None
    parts = resource_name.split("/")
    for index, part in enumerate(parts):
        if part == "conferenceRecords" and index + 1 < len(parts):
            return f"conferenceRecords/{parts[index + 1]}"
    return None


def is_file_generated_state(state) -> bool:
    if state is None:
        return False
    if isinstance(state, str):
        return state == "FILE_GENERATED" or state.endswith("FILE_GENERATED")
    name = getattr(state, "name", None)
    if name:
        return name == "FILE_GENERATED"
    return "FILE_GENERATED" in str(state)


def check_recordings_ready(conference_record: str, credentials: Credentials, user_email: str) -> bool:
    if conference_record not in ENDED_CONFERENCE_RECORDS:
        return False

    if conference_record in RECORDINGS_READY_FOR:
        return True

    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    recordings = list(client.list_recordings(parent=conference_record))

    if not recordings or all(is_file_generated_state(recording.state) for recording in recordings):
        RECORDINGS_READY_FOR.add(conference_record)
        RECORDING_FILE_IDS[conference_record] = [recording.drive_destination.file for recording in recordings or []]
        logger.debug("録画生成完了: %s", conference_record)
        check_all_file_ready(conference_record, credentials, user_email)
        return True
    return False


def check_transcripts_ready(conference_record: str, credentials: Credentials, user_email: str) -> bool:
    if conference_record not in ENDED_CONFERENCE_RECORDS:
        return False

    if conference_record in TRANSCRIPTS_READY_FOR:
        return True

    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    transcripts = list(client.list_transcripts(parent=conference_record))

    if not transcripts or all(is_file_generated_state(transcript.state) for transcript in transcripts):
        TRANSCRIPTS_READY_FOR.add(conference_record)
        TRANSCRIPT_FILE_IDS[conference_record] = [transcript.docs_destination.document for transcript in transcripts or []]
        logger.debug("文字起こし生成完了: %s", conference_record)
        check_all_file_ready(conference_record, credentials, user_email)
        return True
    return False

def check_all_file_ready(conference_record: str, credentials: Credentials, user_email: str) -> None:
    if (
        conference_record in RECORDINGS_READY_FOR
        and conference_record in TRANSCRIPTS_READY_FOR
    ):
        recording_ids = list(dict.fromkeys(RECORDING_FILE_IDS.get(conference_record, [])))
        transcript_ids = list(dict.fromkeys(TRANSCRIPT_FILE_IDS.get(conference_record, [])))
        logger.info(
            "録画と文字起こし完了: %s (recordings=%s, transcripts=%s)",
            conference_record,
            recording_ids,
            transcript_ids,
        )
        threading.Thread(
            target=on_all_file_generated,
            args=(conference_record, recording_ids, transcript_ids, credentials, user_email),
            daemon=True,
        ).start()
        cleanup_conference_state(conference_record)

def cleanup_conference_state(conference_record: str) -> None:
    if (
        conference_record in RECORDINGS_READY_FOR
        and conference_record in TRANSCRIPTS_READY_FOR
    ):
        ENDED_CONFERENCE_RECORDS.discard(conference_record)
        RECORDINGS_READY_FOR.discard(conference_record)
        TRANSCRIPTS_READY_FOR.discard(conference_record)
        RECORDING_FILE_IDS.pop(conference_record, None)
        TRANSCRIPT_FILE_IDS.pop(conference_record, None)
        logger.debug("会議状態をクリア: %s", conference_record)


def on_conference_ended(payload: dict, credentials: Credentials, user_email: str):
    resource_name = payload.get("conferenceRecord", {}).get("name")
    if not resource_name:
        logger.warning("conferenceRecord が見つかりません")
        return
    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    conference = client.get_conference_record(name=resource_name)
    participants = list(client.list_participants(parent=resource_name))
    if not participants:
        logger.info(
            "参加者がいない会議のためスキップ: %s (時刻=%s)",
            conference.name,
            format_timestamp(conference.end_time),
        )
        return
    logger.info(
        "会議終了を検知: %s (時刻=%s, 参加者=%s)",
        conference.name,
        format_timestamp(conference.end_time),
        len(participants),
    )
    ENDED_CONFERENCE_RECORDS.add(resource_name)
    threading.Thread(
        target=on_conference_ended_callback,
        args=(resource_name, credentials, user_email),
        daemon=True,
    ).start()
    check_recordings_ready(resource_name, credentials, user_email)
    check_transcripts_ready(resource_name, credentials, user_email)


def on_recording_event(payload: dict, event_type: str, event_time: str, credentials: Credentials, user_email: str):
    resource_name = payload.get("recording", {}).get("name")
    if not resource_name:
        logger.warning("recording が見つかりません")
        return
    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    recording = client.get_recording(name=resource_name)
    if event_type.endswith("fileGenerated"):
        conference_record = get_conference_record_from_child(recording.name)
        if conference_record and conference_record not in ENDED_CONFERENCE_RECORDS:
            return
        if conference_record and conference_record in RECORDINGS_READY_FOR:
            return
        formatted_time = format_event_time(event_time)
        logger.debug(
            "録画生成: %s (時刻=%s)",
            recording.drive_destination.export_uri,
            formatted_time,
        )
        if conference_record:
            check_recordings_ready(conference_record, credentials, user_email)


def on_transcript_event(payload: dict, event_type: str, event_time: str, credentials: Credentials, user_email: str):
    resource_name = payload.get("transcript", {}).get("name")
    if not resource_name:
        logger.warning("transcript が見つかりません")
        return
    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    transcript = client.get_transcript(name=resource_name)
    if event_type.endswith("fileGenerated"):
        conference_record = get_conference_record_from_child(transcript.name)
        if conference_record and conference_record not in ENDED_CONFERENCE_RECORDS:
            return
        if conference_record and conference_record in TRANSCRIPTS_READY_FOR:
            return
        formatted_time = format_event_time(event_time)
        logger.debug(
            "文字起こし生成: %s (時刻=%s)",
            transcript.docs_destination.export_uri,
            formatted_time,
        )
        if conference_record:
            check_transcripts_ready(conference_record, credentials, user_email)


def format_event_time(value: str) -> str:
    if not value:
        return ""

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    jst = timezone(timedelta(hours=9))
    return parsed.astimezone(jst).strftime("%Y/%m/%d %H:%M:%S.%f")


def format_timestamp(value) -> str:
    if value is None:
        return ""
    try:
        return format_event_time(value.rfc3339())
    except Exception:
        return str(value)


def on_message(message: pubsub_v1.subscriber.message.Message) -> None:
    event_type = message.attributes.get("ce-type", "")
    event_time = message.attributes.get("ce-time", "")
    event_id = message.attributes.get("ce-id", "")
    event_subject = message.attributes.get("ce-subject", "")
    try:
        payload = json.loads(message.data)
    except Exception:
        payload = {}

    try:
        # 処理済みのイベントは無視する
        if event_id:
            if event_id in SEEN_EVENT_ID_SET:
                message.ack()
                return
            if len(SEEN_EVENT_IDS) == SEEN_EVENT_IDS.maxlen:
                oldest = SEEN_EVENT_IDS.popleft()
                SEEN_EVENT_ID_SET.discard(oldest)
            SEEN_EVENT_IDS.append(event_id)
            SEEN_EVENT_ID_SET.add(event_id)

        user_email = get_event_user_email(event_subject)
        if event_type.startswith("google.workspace.events.subscription.v1."):
            handle_subscription_lifecycle(event_type, payload, event_subject)
        elif event_type == "google.workspace.meet.conference.v2.ended":
            credentials = get_event_credentials(event_subject)
            if credentials is None:
                message.ack()
                return
            on_conference_ended(payload, credentials, user_email)
        elif event_type.startswith("google.workspace.meet.recording.v2."):
            credentials = get_event_credentials(event_subject)
            if credentials is None:
                message.ack()
                return
            on_recording_event(payload, event_type, event_time, credentials, user_email)
        elif event_type.startswith("google.workspace.meet.transcript.v2."):
            credentials = get_event_credentials(event_subject)
            if credentials is None:
                message.ack()
                return
            on_transcript_event(payload, event_type, event_time, credentials, user_email)
        else:
            formatted_time = format_event_time(event_time)
            logger.debug(
                "未対応イベント: %s (id=%s, time=%s)",
                event_type,
                event_id,
                formatted_time,
            )
        message.ack()
    except Exception as error:
        logger.exception("イベント処理に失敗しました")
        message.nack()


def listen_for_events(subscription_name: str):
    subscriber = pubsub_v1.SubscriberClient(credentials=PUBSUB_CREDENTIALS)
    stop_event = False

    def _handle_sigint(signum, frame):
        nonlocal stop_event
        stop_event = True

    signal.signal(signal.SIGINT, _handle_sigint)

    with subscriber:
        future = subscriber.subscribe(subscription_name, callback=on_message)
        logger.info("イベント待機中 (Ctrl+Cで停止)")
        try:
            while not stop_event:
                try:
                    future.result(timeout=1.0)
                except TimeoutError:
                    pass
        finally:
            future.cancel()
            subscriber.close()
    logger.info("終了")


def get_conference_info(conference_record: str, credentials: Credentials) -> tuple[str, str, dict]:
    """会議コード、スペース名、ConferenceRecordの各種情報を取得する。"""
    client = meet.ConferenceRecordsServiceClient(credentials=credentials)
    conference = client.get_conference_record(name=conference_record)
    space_name = conference.space or ""
    meeting_code = ""
    if space_name:
        try:
            spaces_client = meet.SpacesServiceClient(credentials=credentials)
            space = spaces_client.get_space(name=space_name)
            meeting_code = space.meeting_code or ""
        except Exception as exc:
            logger.error("スペース情報取得失敗: %s", exc)
    conference_info = {
        "name": conference.name or "",
        "start_time": format_timestamp(conference.start_time),
        "end_time": format_timestamp(conference.end_time),
        "expire_time": format_timestamp(conference.expire_time),
        "space": space_name,
    }
    return meeting_code, space_name, conference_info


def get_webhook_timeout() -> float:
    try:
        value = float(WEBHOOK_TIMEOUT)
    except ValueError:
        logger.warning("WEBHOOK_TIMEOUT が不正なためデフォルト(30秒)を使用: %s", WEBHOOK_TIMEOUT)
        return 30.0
    if value <= 0:
        logger.warning("WEBHOOK_TIMEOUT が0以下のためデフォルト(30秒)を使用: %s", WEBHOOK_TIMEOUT)
        return 30.0
    return value

# Webhookに送信されるJSONの例
# {
#   "event": "conference_ended" | "files_generated",
#   "meeting_code": "...",
#   "organizer_email": "...",
#   "conference_record": {
#     "name": "conferenceRecords/...",
#     "start_time": "2026/04/28 10:00:00.000000",
#     "end_time": "2026/04/28 11:00:00.000000",
#     "expire_time": "...",
#     "space": "spaces/..."
#   },
#   "recording_ids": [...],   // files_generated のみ
#   "transcript_ids": [...]   // files_generated のみ
# }
def post_webhook(body: dict) -> None:
    if not WEBHOOK_URL:
        logger.debug("WEBHOOK_URL が未設定のためスキップ: %s", body.get("event"))
        return
    if not WEBHOOK_SHARED_SECRET:
        logger.error("WEBHOOK_SHARED_SECRET が未設定のため Webhook 送信をスキップ: %s", body.get("event"))
        return
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.".encode("utf-8") + body_bytes
    signature = hmac.new(
        WEBHOOK_SHARED_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": f"sha256={signature}",
    }
    try:
        response = requests.post(WEBHOOK_URL, data=body_bytes, headers=headers, timeout=get_webhook_timeout())
        if response.status_code >= 400:
            logger.error(
                "Webhook送信失敗: event=%s status=%s body=%s",
                body.get("event"),
                response.status_code,
                response.text,
            )
        else:
            logger.info("Webhook送信完了: event=%s", body.get("event"))
    except Exception:
        logger.exception("Webhook送信で例外が発生しました")


# 会議終了を検知したら呼び出されるコールバック(別スレッドで動いている)
def on_conference_ended_callback(
    conference_record: str,
    credentials: Credentials,
    user_email: str,
) -> None:
    meeting_code, space_name, conference_info = get_conference_info(conference_record, credentials)
    post_webhook({
        "event": "conference_ended",
        "meeting_code": meeting_code,
        "space_name": space_name,
        "organizer_email": user_email,
        "conference_record": conference_info,
    })

# 会議の全ファイルが生成されたら呼び出されるコールバック(別スレッドで動いている)
def on_all_file_generated(
    conference_record: str,
    recording_ids: list[str],
    transcript_ids: list[str],
    credentials: Credentials,
    user_email: str,
) -> None:
    meeting_code, space_name, conference_info = get_conference_info(conference_record, credentials)
    post_webhook({
        "event": "files_generated",
        "meeting_code": meeting_code,
        "organizer_email": user_email,
        "conference_record": conference_info,
        "recording_ids": recording_ids,
        "transcript_ids": transcript_ids,
    })


def main():
    global WORKSPACE_SUBSCRIPTION_NAME

    if not PROJECT_ID:
        logger.error("環境変数 PROJECT_ID を設定してください")
        sys.exit(1)

    if not PUBSUB_TOPIC_ID:
        logger.error("環境変数 PUBSUB_TOPIC_ID を設定してください")
        sys.exit(1)

    if not PUBSUB_SUBSCRIPTION_ID:
        logger.error("環境変数 PUBSUB_SUBSCRIPTION_ID を設定してください")
        sys.exit(1)

    if not ADMIN_USER_EMAIL:
        logger.error("環境変数 ADMIN_USER_EMAIL を設定してください")
        sys.exit(1)

    users = list_org_user_ids()
    if not users:
        logger.error("組織ユーザーの取得に失敗しました")
        sys.exit(1)
    logger.info("組織ユーザー数: %s", len(users))

    failures = 0
    for user_id, email in users:
        if not ensure_subscription_for_user(user_id, email):
            failures += 1

    if failures:
        logger.warning("サブスクリプション作成失敗: %s件", failures)

    logger.info("サブスクリプション作成完了。待機開始")
    listen_for_events(subscription_name=SUBSCRIPTION_NAME)
    return


if __name__ == "__main__":
    main()
