import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))
PREFIX_MAPPING_PATH = os.environ.get("PREFIX_MAPPING_PATH", "./data/meetbot_mapping.json")

_ENV_KEYS_TO_CLEAR = [
    "SERVICE_ACCOUNT_JSON",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "WEBHOOK_SHARED_SECRET",
]
for _key in _ENV_KEYS_TO_CLEAR:
    os.environ.pop(_key, None)

# プレフィックス→移動先Driveフォルダ/Slackチャンネル設定 (Socket Mode で管理者のみが更新)
# 永続化ファイルから起動時にロードされる。
PREFIX_MAPPING: dict[str, dict[str, str]] = {}

# PREFIX_MAPPING の読み書きを保護するロック
_PREFIX_MAPPING_LOCK = threading.Lock()


def load_prefix_mapping() -> None:
    """永続化されたプレフィックス設定をロードしてメモリ上のマッピングへ反映。"""
    path = PREFIX_MAPPING_PATH
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("PREFIX_MAPPING の読み込みに失敗: %s", exc)
        return
    if not isinstance(data, dict):
        logger.error("PREFIX_MAPPING の内容が辞書形式ではありません: %s", type(data).__name__)
        return
    cleaned: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        cleaned[key] = {
            "drive_folder_id": str(value.get("drive_folder_id", "") or ""),
            "slack_channel": str(value.get("slack_channel", "") or ""),
        }
    with _PREFIX_MAPPING_LOCK:
        PREFIX_MAPPING.clear()
        PREFIX_MAPPING.update(cleaned)
    logger.info("PREFIX_MAPPING をロード: %s 件 (%s)", len(cleaned), path)


def save_prefix_mapping_locked() -> None:
    """原子的書き込みで永続化。呼び出し側で _PREFIX_MAPPING_LOCK を保持していること。"""
    path = PREFIX_MAPPING_PATH
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    serialized = json.dumps(PREFIX_MAPPING, ensure_ascii=False, indent=2)
    # 同じディレクトリに tmp を作って fsync → os.replace で原子的に差し替える
    fd, tmp_path = tempfile.mkstemp(prefix=".prefix_mapping.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # 失敗時は tmp を掃除
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_prefix_mapping_snapshot() -> dict[str, dict[str, str]]:
    with _PREFIX_MAPPING_LOCK:
        return {k: dict(v) for k, v in PREFIX_MAPPING.items()}


def set_prefix_mapping_entry(prefix: str, drive_folder_id: str, slack_channel: str) -> None:
    with _PREFIX_MAPPING_LOCK:
        PREFIX_MAPPING[prefix] = {
            "drive_folder_id": drive_folder_id,
            "slack_channel": slack_channel,
        }
        save_prefix_mapping_locked()


def remove_prefix_mapping_entry(prefix: str) -> bool:
    with _PREFIX_MAPPING_LOCK:
        if prefix not in PREFIX_MAPPING:
            return False
        del PREFIX_MAPPING[prefix]
        save_prefix_mapping_locked()
        return True

SLACK_API_BASE = "https://slack.com/api"

# (HH:MM:SS) の時刻表記を検出するパターン
TIMESTAMP_PATTERN = re.compile(r"\((\d{1,2}):(\d{2}):(\d{2})\)")
BARE_TIMESTAMP_PATTERN = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")

# Gemini が議事録末尾・本文に付与する除外対象フレーズ
GEMINI_NOISE_PHRASES = [
    "この要約を評価する",
    "Gemini が生成したメモの内容の正確性をご確認ください",
    "Gemini によるメモ作成のヒント",
    "これらのメモの品質はいかがでしたか",
    "簡単なアンケートで、メモがニーズに合っていたかどうかなど、フィードバックをお寄せください",
]

# Drive ファイル名から会議名を抽出するためのパターン
RECORDING_NAME_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*[-–]\s*\d{4}[/-]\d{2}[/-]\d{2}"
)
# デフォルトタイトル(ミーティングコードのみ)を検出するパターン
# 例: "wpi-xbcv-soe (2026-03-18 02:27 GMT+9)"
DEFAULT_MEETING_CODE_PATTERN = re.compile(
    r"^(?P<code>[a-z]{3,}-[a-z]{3,}-[a-z]{3,})\s*\("
)
# トランスクリプト (Gemini によるメモ) のパターン
TRANSCRIPT_NAME_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*[-–]\s*\d{4}[/-]\d{2}[/-]\d{2}.*?[-–]\s*Gemini によるメモ"
)
# 「会議名が未設定」のトランスクリプトパターン
# 例: " 2026/04/28 16:54 JST に開始した会議 - Gemini によるメモ"
UNTITLED_TRANSCRIPT_PATTERN = re.compile(
    r"^\s*\d{4}[/-]\d{2}[/-]\d{2}.*に開始した会議"
)
# "[xxx]yyy" または "[xxx] yyy" 形式のプレフィックス抽出 (] の後の空白は任意)
PREFIX_BRACKET_PATTERN = re.compile(r"^\s*\[(?P<prefix>[^\]]+)\]\s*(?P<rest>.*)$")


def load_base_credentials() -> Credentials:
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "サービスアカウントの認証情報が環境変数SERVICE_ACCOUNT_JSONに設定されていません"
        )
    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SERVICE_ACCOUNT_JSON の形式が不正です。") from exc
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


BASE_CREDENTIALS = load_base_credentials()


def get_delegated_credentials(user_email: str) -> Credentials:
    if not user_email:
        raise RuntimeError("organizer_email が空のため成りすまし資格情報を作成できません")
    return BASE_CREDENTIALS.with_subject(user_email)


def get_drive_service(credentials: Credentials):
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def get_docs_service(credentials: Credentials):
    return build("docs", "v1", credentials=credentials, cache_discovery=False)


def get_drive_file_name(drive_service, file_id: str) -> str | None:
    try:
        resp = drive_service.files().get(
            fileId=file_id,
            fields="name",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        logger.error("Driveファイル名取得失敗: file_id=%s %s", file_id, exc)
        return None
    return resp.get("name")


def extract_meeting_name_from_recording(file_name: str) -> str | None:
    match = DEFAULT_MEETING_CODE_PATTERN.match(file_name)
    if match:
        return match.group("code").strip()
    match = RECORDING_NAME_PATTERN.match(file_name)
    if match:
        return match.group("title").strip()
    return None


def extract_meeting_name_from_transcript(file_name: str) -> str | None:
    if UNTITLED_TRANSCRIPT_PATTERN.match(file_name):
        return "会議"
    match = TRANSCRIPT_NAME_PATTERN.match(file_name)
    if match:
        return match.group("title").strip()
    return None


def resolve_meeting_name(
    drive_service,
    recording_ids: list[str],
    transcript_ids: list[str],
) -> str:
    for rid in recording_ids:
        name = get_drive_file_name(drive_service, rid)
        if not name:
            continue
        meeting = extract_meeting_name_from_recording(name)
        if meeting:
            return meeting
    for tid in transcript_ids:
        name = get_drive_file_name(drive_service, tid)
        if not name:
            continue
        meeting = extract_meeting_name_from_transcript(name)
        if meeting:
            return meeting
    return "会議"


def extract_prefix(meeting_name: str) -> tuple[str | None, str]:
    match = PREFIX_BRACKET_PATTERN.match(meeting_name)
    if not match:
        return None, meeting_name
    inner = match.group("prefix").strip()
    prefix = f"[{inner}]"
    return prefix, match.group("rest").strip() or meeting_name


def move_files_to_folder(
    drive_service,
    file_ids: list[str],
    folder_id: str,
) -> bool:
    if not folder_id:
        return False
    success = True
    for fid in file_ids:
        try:
            current = drive_service.files().get(
                fileId=fid,
                fields="parents",
                supportsAllDrives=True,
            ).execute()
            prev_parents = ",".join(current.get("parents", []))
            drive_service.files().update(
                fileId=fid,
                addParents=folder_id,
                removeParents=prev_parents,
                fields="id, parents",
                supportsAllDrives=True,
            ).execute()
            logger.info("Driveファイル移動: file_id=%s -> folder=%s", fid, folder_id)
        except HttpError as exc:
            logger.error("Driveファイル移動失敗: file_id=%s %s", fid, exc)
            success = False
    return success


def fetch_transcript_document(docs_service, document_id: str) -> dict[str, Any] | None:
    try:
        return docs_service.documents().get(documentId=document_id).execute()
    except HttpError as exc:
        logger.error("Docs取得失敗: document_id=%s %s", document_id, exc)
        return None


def build_drive_video_url(recording_id: str, seconds: int) -> str:
    return f"https://drive.google.com/file/d/{recording_id}/view?usp=drive_web&t={seconds}"


def timestamp_to_seconds(hh: str, mm: str, ss: str) -> int:
    return int(hh) * 3600 + int(mm) * 60 + int(ss)


HEADING_STYLES = {"TITLE", "HEADING_1", "HEADING_2", "HEADING_3", "HEADING_4", "HEADING_5", "HEADING_6", "SUBTITLE"}


def is_noise_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    for phrase in GEMINI_NOISE_PHRASES:
        if phrase in stripped:
            return True
    return False


def mrkdwn_escape(text: str) -> str:
    # Slack mrkdwn の <, >, & をエスケープ
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def convert_text_with_timestamps(text: str, recording_id: str | None) -> str:
    """本文中の (HH:MM:SS) を Drive 動画の該当秒にジャンプするリンクへ変換。"""
    if not recording_id:
        return mrkdwn_escape(text)

    result: list[str] = []
    last = 0
    for m in TIMESTAMP_PATTERN.finditer(text):
        result.append(mrkdwn_escape(text[last:m.start()]))
        hh, mm, ss = m.group(1), m.group(2), m.group(3)
        seconds = timestamp_to_seconds(hh, mm, ss)
        url = build_drive_video_url(recording_id, seconds)
        label = f"({hh}:{mm}:{ss})"
        result.append(f"<{url}|{label}>")
        last = m.end()
    result.append(mrkdwn_escape(text[last:]))
    return "".join(result)


def render_element(el: dict[str, Any], recording_id: str | None) -> str:
    """textRun / person / richLink を mrkdwn 文字列へ変換。"""
    text_run = el.get("textRun")
    if text_run is not None:
        content = text_run.get("content", "")
        style = text_run.get("textStyle", {}) or {}
        link = style.get("link") or {}
        link_url = link.get("url")
        # 素の "HH:MM:SS" で link が付いている textRun は動画タイムスタンプとして扱う
        bare_ts = BARE_TIMESTAMP_PATTERN.match(content) if link else None
        if bare_ts and recording_id:
            hh, mm, ss = bare_ts.group(1), bare_ts.group(2), bare_ts.group(3)
            seconds = timestamp_to_seconds(hh, mm, ss)
            url = build_drive_video_url(recording_id, seconds)
            return f"<{url}|{hh}:{mm}:{ss}>"
        if link_url:
            label = convert_text_with_timestamps(content, recording_id)
            return f"<{link_url}|{label}>"
        return convert_text_with_timestamps(content, recording_id)

    person = el.get("person")
    if person is not None:
        props = person.get("personProperties", {}) or {}
        email = props.get("email") or ""
        name = props.get("name") or ""
        if email:
            slack_user_id = slack_user_id_for_email_cached(email)
            if slack_user_id:
                return f"<@{slack_user_id}>"
        if name:
            return mrkdwn_escape(f"`{name}`")
        return mrkdwn_escape(email or name)

    rich = el.get("richLink")
    if rich is not None:
        props = rich.get("richLinkProperties", {}) or {}
        title = props.get("title") or ""
        uri = props.get("uri") or ""
        if uri and title:
            return f"<{uri}|{mrkdwn_escape(title)}>"
        if uri:
            return f"<{uri}>"
        return mrkdwn_escape(title)

    return ""


def split_paragraph_into_lines(
    paragraph: dict[str, Any],
    recording_id: str | None,
) -> list[tuple[bool, str]]:
    """段落を (is_bold_heading, rendered_mrkdwn) の行リストへ分解する。

    - 段落内で bold=true の textRun は独立した小見出し行として切り出す
    -  (vertical tab) と \n を行区切りとして扱う
    """
    elements = paragraph.get("elements", []) or []
    # セグメント: (is_bold, text) をフラット列にする
    segments: list[tuple[bool, str]] = []
    for el in elements:
        rendered = render_element(el, recording_id)
        if not rendered:
            continue
        text_run = el.get("textRun")
        is_bold = bool((text_run or {}).get("textStyle", {}).get("bold")) if text_run else False
        segments.append((is_bold, rendered))

    # セグメントを繋げて、 と \n で行に分割。bold 境界も行の切れ目として扱う
    lines: list[tuple[bool, str]] = []
    current_bold: bool | None = None
    current_buf: list[str] = []

    def flush():
        if not current_buf:
            return
        text = "".join(current_buf)
        if current_bold:
            cleaned = text.strip().strip(":：").strip()
            if cleaned:
                lines.append((True, cleaned))
        else:
            stripped = text
            # 直前の行が bold (小見出し) の場合、先頭のコロン/空白を削除する
            if lines and lines[-1][0]:
                stripped = stripped.lstrip()
                stripped = re.sub(r"^[:：]\s*", "", stripped)
            if stripped.strip():
                lines.append((False, stripped))

    for is_bold, text in segments:
        # bold 状態が変わったら行を確定
        if current_bold is None:
            current_bold = is_bold
        if is_bold != current_bold:
            flush()
            current_buf = []
            current_bold = is_bold

        # テキスト内の  / \n をさらに行区切りとして分解
        parts = re.split(r"[\n]", text)
        for i, part in enumerate(parts):
            current_buf.append(part)
            if i < len(parts) - 1:
                flush()
                current_buf = []
    flush()
    return lines


UNAVAILABLE_RECORDING_PATTERN = re.compile(r"\(一部の録画は利用できません\)")


def replace_unavailable_recording_in_paragraph(
    paragraph: dict[str, Any],
    recording_ids: list[str],
) -> dict[str, Any]:
    """段落内の "(一部の録画は利用できません)" テキストを recording_ids のリンク要素で差し替える。

    元の paragraph は変更せず、差し替えが必要な場合のみ新しい dict を返す。
    差し替え対象が無い場合は元の paragraph をそのまま返す。
    """
    if not recording_ids:
        return paragraph
    elements = paragraph.get("elements", []) or []
    new_elements: list[dict[str, Any]] = []
    replaced = False
    for el in elements:
        text_run = el.get("textRun")
        if text_run is None:
            new_elements.append(el)
            continue
        content = text_run.get("content", "")
        if not UNAVAILABLE_RECORDING_PATTERN.search(content):
            new_elements.append(el)
            continue
        # マッチした textRun は recording_ids の richLink 相当に差し替える
        replaced = True
        for i, rid in enumerate(recording_ids):
            if i > 0:
                new_elements.append({"textRun": {"content": " ", "textStyle": {}}})
            new_elements.append(
                {
                    "richLink": {
                        "richLinkProperties": {
                            "title": "録画",
                            "uri": f"https://drive.google.com/file/d/{rid}/view?usp=drive_web",
                            "mimeType": "video/mp4",
                        }
                    }
                }
            )
        # 末尾の改行は維持する
        if content.endswith("\n"):
            new_elements.append({"textRun": {"content": "\n", "textStyle": {}}})
    if not replaced:
        return paragraph
    new_paragraph = dict(paragraph)
    new_paragraph["elements"] = new_elements
    return new_paragraph


def doc_to_slack_blocks(
    document: dict[str, Any],
    recording_id: str | None,
    meeting_name: str,
    recording_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    content = document.get("body", {}).get("content", [])
    # rendered_lines: list of (kind, text)  kind in {"heading", "subheading", "text"}
    rendered_lines: list[tuple[str, str]] = []

    # 大見出しに分類するスタイル(会議タイトル直下の大セクション)
    MAJOR_HEADING_STYLES = {"TITLE", "HEADING_1", "HEADING_2"}

    effective_recording_ids = recording_ids or ([recording_id] if recording_id else [])

    for element in content:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        paragraph = replace_unavailable_recording_in_paragraph(paragraph, effective_recording_ids)
        style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        paragraph_plain = "".join(
            (el.get("textRun") or {}).get("content", "") for el in paragraph.get("elements", [])
        )
        if is_noise_line(paragraph_plain):
            continue
        para_lines = split_paragraph_into_lines(paragraph, recording_id)
        for is_bold_inline, text in para_lines:
            if is_noise_line(text):
                continue
            if style in MAJOR_HEADING_STYLES:
                rendered_lines.append(("major_heading", text))
            elif style in HEADING_STYLES:
                rendered_lines.append(("heading", text))
            elif is_bold_inline:
                rendered_lines.append(("subheading", text))
            else:
                rendered_lines.append(("text", text))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📝 議事録: {meeting_name}"[:150]},
        }
    ]

    CHUNK_LIMIT = 2900
    buffer: list[str] = []
    buffer_len = 0

    def flush_buffer() -> None:
        nonlocal buffer, buffer_len
        while buffer and buffer[-1] == "":
            buffer.pop()
        if not buffer:
            buffer = []
            buffer_len = 0
            return
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(buffer)},
            }
        )
        buffer = []
        buffer_len = 0

    def append_group(group_lines: list[str]) -> None:
        """小見出し + 直後の本文をまとめた単位を追加する。
        小見出しと本文が分断されないよう、バッファに入り切らない場合は flush し、
        それでも入らないときだけグループ内で分割する。
        """
        nonlocal buffer, buffer_len
        if not group_lines:
            return
        group_len = sum(len(line) + 1 for line in group_lines)
        # バッファに収まらない場合はまず flush
        if buffer and buffer_len + group_len > CHUNK_LIMIT:
            flush_buffer()
        # flush 後もグループ単体でチャンク上限を超える場合は、グループ内を小見出し単位で分割できないので
        # やむなくそのまま流し込む(Slack 側では 3000 文字を超えると送信できないため更に細分化)
        if group_len > CHUNK_LIMIT:
            for line in group_lines:
                line_len = len(line) + 1
                if buffer and buffer_len + line_len > CHUNK_LIMIT:
                    flush_buffer()
                buffer.append(line)
                buffer_len += line_len
            return
        for line in group_lines:
            buffer.append(line)
            buffer_len += len(line) + 1

    # rendered_lines を「小見出し(または無し) + 直後の本文」のグループ列に変換する
    # groups の要素は ("header", kind, text) または ("lines", list[str])
    groups: list[tuple] = []
    current_lines: list[str] = []

    def push_current_lines() -> None:
        nonlocal current_lines
        if current_lines:
            groups.append(("lines", current_lines))
        current_lines = []

    for kind, text in rendered_lines:
        if kind in ("major_heading", "heading"):
            push_current_lines()
            groups.append(("header", kind, text))
        elif kind == "subheading":
            push_current_lines()
            current_lines.append(f"*{text}*")
        else:
            current_lines.append(text)
    push_current_lines()

    for group in groups:
        if group[0] == "header":
            _, kind, text = group
            flush_buffer()
            if kind == "major_heading":
                blocks.append(
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"📌 {text}"[:150]},
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": text[:150]},
                    }
                )
        else:
            _, lines = group
            append_group(lines)

    flush_buffer()
    return blocks


def slack_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN が未設定です")
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.error("Slack API エラー: url=%s response=%s", url, data)
    return data


def slack_lookup_user_by_email(email: str) -> str | None:
    if not SLACK_BOT_TOKEN:
        logger.warning("SLACK_BOT_TOKEN 未設定のためユーザー参照不可")
        return None
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(
        f"{SLACK_API_BASE}/users.lookupByEmail",
        headers=headers,
        params={"email": email},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error("Slack users.lookupByEmail 失敗: %s", data)
        return None
    return data.get("user", {}).get("id")


def slack_user_is_admin(user_id: str) -> bool:
    """users.info で is_admin / is_owner / is_primary_owner のいずれかが true ならtrue。"""
    if not SLACK_BOT_TOKEN or not user_id:
        return False
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(
        f"{SLACK_API_BASE}/users.info",
        headers=headers,
        params={"user": user_id},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error("Slack users.info 失敗: %s", data)
        return False
    user = data.get("user", {}) or {}
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))


# 議事録変換中に同じメアドを複数回叩かないためのキャッシュ。
# 値が None の場合は「ルックアップ済みだが見つからなかった」ことを表す。
_SLACK_USER_ID_CACHE: dict[str, str | None] = {}


def slack_user_id_for_email_cached(email: str) -> str | None:
    if not email:
        return None
    key = email.lower()
    if key in _SLACK_USER_ID_CACHE:
        return _SLACK_USER_ID_CACHE[key]
    user_id = slack_lookup_user_by_email(email)
    _SLACK_USER_ID_CACHE[key] = user_id
    return user_id


def slack_open_im(user_id: str) -> str | None:
    data = slack_post(
        f"{SLACK_API_BASE}/conversations.open",
        {"users": user_id},
    )
    if not data.get("ok"):
        return None
    return data.get("channel", {}).get("id")


SLACK_BLOCKS_PER_MESSAGE = 50


def _split_blocks_for_slack(
    blocks: list[dict[str, Any]],
    limit: int = SLACK_BLOCKS_PER_MESSAGE,
) -> list[list[dict[str, Any]]]:
    """Slack の 1 メッセージあたり Block 上限に合わせて分割する。

    できるだけ見出し(header block)の直前で切ることで、小見出しと本文の対応関係を崩さない。
    """
    if len(blocks) <= limit:
        return [blocks]

    chunks: list[list[dict[str, Any]]] = []
    i = 0
    n = len(blocks)
    while i < n:
        end = min(i + limit, n)
        if end < n:
            # end の位置より前にある最後の header block を探してそこで切る
            split_at = end
            for j in range(end, i, -1):
                if blocks[j - 1].get("type") == "header":
                    split_at = j - 1
                    break
            if split_at <= i:
                # 切りどころが見つからなければ強制的に limit で切る
                split_at = end
            chunks.append(blocks[i:split_at])
            i = split_at
        else:
            chunks.append(blocks[i:end])
            i = end
    return chunks


def slack_post_message(
    channel: str,
    blocks: list[dict[str, Any]],
    fallback_text: str,
) -> bool:
    chunks = _split_blocks_for_slack(blocks)
    thread_ts: str | None = None
    for idx, chunk in enumerate(chunks):
        body: dict[str, Any] = {
            "channel": channel,
            "blocks": chunk,
            "text": fallback_text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            body["thread_ts"] = thread_ts
            # スレッドに投稿しつつチャンネルにも表示する
            body["reply_broadcast"] = True
        data = slack_post(f"{SLACK_API_BASE}/chat.postMessage", body)
        if not data.get("ok"):
            return False
        if idx == 0:
            thread_ts = data.get("ts")
            if not thread_ts:
                logger.error("初回 Slack 投稿の ts が取得できないためスレッド化を中断")
                return False
    return True


def handle_files_generated(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    transcript_ids: list[str] = payload.get("transcript_ids") or []
    if not transcript_ids:
        logger.info("transcript_ids が空のため終了")
        return {"status": "skipped", "reason": "no_transcript"}, 200

    organizer_email: str = payload.get("organizer_email") or ""
    if not organizer_email:
        logger.error("organizer_email が空です")
        return {"status": "error", "reason": "missing_organizer_email"}, 400

    recording_ids: list[str] = payload.get("recording_ids") or []

    credentials = get_delegated_credentials(organizer_email)
    drive_service = get_drive_service(credentials)
    docs_service = get_docs_service(credentials)

    meeting_name = resolve_meeting_name(drive_service, recording_ids, transcript_ids)
    logger.info("会議名: %s", meeting_name)

    # 議事録本文の取得と Slack Block 変換 (先頭の transcript を使用)
    primary_transcript_id = transcript_ids[0]
    document = fetch_transcript_document(docs_service, primary_transcript_id)
    if document is None:
        return {"status": "error", "reason": "transcript_fetch_failed"}, 500

    primary_recording_id = recording_ids[0] if recording_ids else None
    blocks = doc_to_slack_blocks(document, primary_recording_id, meeting_name, recording_ids)

    # プレフィックス抽出 → ファイル移動
    prefix, _ = extract_prefix(meeting_name)
    mapping_snapshot = get_prefix_mapping_snapshot()
    target_config = mapping_snapshot.get(prefix) if prefix else None

    effective_prefix: str | None = prefix
    if target_config:
        folder_id = target_config.get("drive_folder_id", "")
        if folder_id:
            moved_ok = move_files_to_folder(
                drive_service,
                recording_ids + transcript_ids,
                folder_id,
            )
            if not moved_ok:
                logger.warning("ファイル移動に失敗したためプレフィックスなしとして扱います: %s", prefix)
                effective_prefix = None
        else:
            logger.info("drive_folder_id が未設定のため移動をスキップ: prefix=%s", prefix)
    else:
        effective_prefix = None

    # Slack 投稿先決定
    fallback_text = f"議事録: {meeting_name}"
    channel: str | None = None
    if effective_prefix:
        mapped = mapping_snapshot.get(effective_prefix, {})
        channel = mapped.get("slack_channel") or None

    if not channel:
        user_id = slack_lookup_user_by_email(organizer_email)
        if not user_id:
            return {"status": "error", "reason": "slack_user_lookup_failed"}, 500
        channel = slack_open_im(user_id)
        if not channel:
            return {"status": "error", "reason": "slack_open_im_failed"}, 500

    if not slack_post_message(channel, blocks, fallback_text):
        return {"status": "error", "reason": "slack_post_failed"}, 500

    return {"status": "ok", "meeting": meeting_name, "channel": channel}, 200


app = Flask(__name__)


# タイムスタンプの許容ずれ(秒)。リプレイ攻撃対策。
WEBHOOK_TIMESTAMP_TOLERANCE_SEC = 300


def verify_webhook_signature(req) -> bool:
    """HMAC-SHA256 による Webhook 署名検証。

    ヘッダ:
      - X-Webhook-Timestamp: Unix epoch 秒
      - X-Webhook-Signature: "sha256=<hex_digest>"
    署名対象: "<timestamp>." + <raw_body_bytes>
    鍵: WEBHOOK_SHARED_SECRET

    未設定時は拒否する(= 運用上必ず設定する必要がある)。
    """
    if not WEBHOOK_SHARED_SECRET:
        logger.error("WEBHOOK_SHARED_SECRET が未設定のため Webhook 受信不可")
        return False

    timestamp = req.headers.get("X-Webhook-Timestamp", "")
    signature = req.headers.get("X-Webhook-Signature", "")
    if not timestamp or not signature:
        return False

    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts_int) > WEBHOOK_TIMESTAMP_TOLERANCE_SEC:
        return False

    body = req.get_data(cache=True) or b""
    expected = hmac.new(
        WEBHOOK_SHARED_SECRET.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def _process_files_generated_async(payload: dict[str, Any]) -> None:
    try:
        body, code = handle_files_generated(payload)
        logger.info("files_generated 処理完了: code=%s body=%s", code, body)
    except Exception:
        logger.exception("files_generated のバックグラウンド処理で例外が発生しました")


@app.post("/webhook")
def webhook():
    if not verify_webhook_signature(request):
        return jsonify({"status": "error", "reason": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    if event != "files_generated":
        logger.debug("対象外イベントをスキップ: %s", event)
        return jsonify({"status": "ignored", "event": event}), 200
    # 呼び出し側は処理結果を必要としないため、重い処理はバックグラウンドに回して即 200 を返す
    threading.Thread(
        target=_process_files_generated_async,
        args=(payload,),
        name="webhook-files-generated",
        daemon=True,
    ).start()
    return jsonify({"status": "accepted"}), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


SLASH_COMMAND_HELP = (
    "*使い方*\n"
    "• `/meetbot list` — 登録されているプレフィックス一覧\n"
    "• `/meetbot set <プレフィックス> <DriveフォルダID> <Slackチャンネル>` — 追加/更新\n"
    "• `/meetbot remove <プレフィックス>` — 削除\n"
    "*チャンネル/フォルダIDに空白を含む場合はダブルクォートで囲んでください*"
)


def _format_prefix_list() -> str:
    snapshot = get_prefix_mapping_snapshot()
    if not snapshot:
        return "プレフィックス設定はありません"
    lines = ["登録されているプレフィックス:"]
    for key in sorted(snapshot.keys()):
        value = snapshot[key]
        folder = value.get("drive_folder_id") or "(未設定)"
        channel = value.get("slack_channel") or "(未設定)"
        lines.append(f"• `{key}` → folder=`{folder}` / channel=`{channel}`")
    return "\n".join(lines)


def handle_prefix_command(user_id: str, text: str) -> str:
    """/meetbot スラッシュコマンドを処理して応答テキスト(ephemeral)を返す。"""
    if not slack_user_is_admin(user_id):
        return "このコマンドはワークスペースの管理者のみ実行できます"

    text = text.strip()
    if not text:
        return SLASH_COMMAND_HELP

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return f"コマンドの解析に失敗しました: {exc}"

    if not tokens:
        return SLASH_COMMAND_HELP

    subcommand = tokens[0].lower()
    args = tokens[1:]

    if subcommand in ("help", "-h", "--help"):
        return SLASH_COMMAND_HELP

    if subcommand == "list":
        return _format_prefix_list()

    if subcommand == "set":
        if len(args) != 3:
            return (
                "引数が不正です。`/meetbot set <プレフィックス> <DriveフォルダID> <Slackチャンネル>` "
                "の形式で指定してください"
            )
        prefix, folder_id, channel = args
        if not prefix:
            return "プレフィックスが空です"
        try:
            set_prefix_mapping_entry(prefix, folder_id, channel)
        except OSError as exc:
            logger.exception("PREFIX_MAPPING の保存に失敗")
            return f"保存に失敗しました: {exc}"
        return f"プレフィックス `{prefix}` を設定しました (folder=`{folder_id}`, channel=`{channel}`)"

    if subcommand == "remove":
        if len(args) != 1:
            return "引数が不正です。`/meetbot remove <プレフィックス>` の形式で指定してください"
        prefix = args[0]
        try:
            removed = remove_prefix_mapping_entry(prefix)
        except OSError as exc:
            logger.exception("PREFIX_MAPPING の保存に失敗")
            return f"保存に失敗しました: {exc}"
        if not removed:
            return f"プレフィックス `{prefix}` は登録されていません"
        return f"プレフィックス `{prefix}` を削除しました"

    return f"不明なサブコマンド: `{subcommand}`\n{SLASH_COMMAND_HELP}"


def _socket_mode_listener(client: SocketModeClient, req: SocketModeRequest) -> None:
    # 受信したら Slack 側に ACK を返す(3秒ルール)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type != "slash_commands":
        return

    payload = req.payload or {}
    command = payload.get("command", "")
    user_id = payload.get("user_id", "")
    text = payload.get("text", "") or ""
    response_url = payload.get("response_url")

    if command != "/meetbot":
        logger.debug("未対応のスラッシュコマンド: %s", command)
        return

    try:
        response_text = handle_prefix_command(user_id, text)
    except Exception:
        logger.exception("スラッシュコマンド処理で例外")
        response_text = "コマンド処理中にエラーが発生しました"

    if response_url:
        try:
            requests.post(
                response_url,
                json={
                    "response_type": "ephemeral",
                    "text": response_text,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": response_text},
                        }
                    ],
                },
                timeout=10,
            )
        except requests.RequestException:
            logger.exception("response_url への応答送信に失敗")


def start_socket_mode() -> None:
    if not SLACK_APP_TOKEN or not SLACK_BOT_TOKEN:
        logger.warning(
            "SLACK_APP_TOKEN または SLACK_BOT_TOKEN が未設定のため Socket Mode を起動しません"
        )
        return
    web_client = WebClient(token=SLACK_BOT_TOKEN)
    sm_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    sm_client.socket_mode_request_listeners.append(_socket_mode_listener)
    sm_client.connect()
    logger.info("Socket Mode に接続しました")


# 起動時に永続化された設定を読み込む
load_prefix_mapping()

# Socket Mode は別スレッドで常駐させる
_SOCKET_MODE_THREAD: threading.Thread | None = None


def _ensure_socket_mode_started() -> None:
    global _SOCKET_MODE_THREAD
    if _SOCKET_MODE_THREAD is not None and _SOCKET_MODE_THREAD.is_alive():
        return
    _SOCKET_MODE_THREAD = threading.Thread(target=start_socket_mode, name="slack-socket-mode", daemon=True)
    _SOCKET_MODE_THREAD.start()


_ensure_socket_mode_started()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
