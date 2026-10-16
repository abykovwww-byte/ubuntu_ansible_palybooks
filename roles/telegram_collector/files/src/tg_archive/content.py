"""Message projection and fixed secret masks. No network or persistence."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from telethon import types

from .discovery import classify_media

MASKS = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----"
        ),
    ),
    ("bearer", re.compile(r"\bAuthorization\s*:\s*Bearer\s+\S+", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]+")),
    ("aws_key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("telegram_bot", re.compile(r"\b\d{5,16}:[A-Za-z0-9_-]{30,}\b")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:пароль|password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*(?!\[REDACTED:)\S+"
        ),
    ),
)

# Platform-independent, explicit table: never consult Windows registry/mimetypes.
MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/json": "json",
    "application/msword": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.visio": "vsd",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
MEDIA_STATE = ("media_status", "media_path", "media_sha256", "media_error", "media_attempts")


def redact(text: str) -> tuple[str, int]:
    count = 0
    for kind, pattern in MASKS:
        text, n = pattern.subn(f"[REDACTED:{kind}]", text)
        count += n
    return text, count


def extension(name: str | None, mime: str | None, kind: str | None) -> str | None:
    suffix = PurePosixPath((name or "").replace("\\", "/")).suffix[1:].lower()
    if suffix and len(suffix) <= 16:
        return suffix
    fixed = {"photo": "jpg", "voice": "ogg", "video": "mp4", "video_note": "mp4"}
    return fixed.get(kind) or MIME_EXT.get((mime or "").lower())


def eligibility(kind, size, spec, settings, protected):
    if protected:
        return "skipped_protected"
    if not spec.media:
        return "skipped_disabled"
    if kind not in settings.media_kinds:
        return "skipped_kind"
    if size is not None and size > settings.max_file_mb * 1024**2:
        return "skipped_too_big"
    return "pending"


def transition(row, previous):
    """Preserve transfer state only for the same attachment; reset replaced/removed."""
    if row["media_kind"] is None:
        return row
    if (
        previous
        and previous["media_kind"] is not None
        and previous["media_file_id"] == row["media_file_id"]
    ):
        status = previous["media_status"]
        for key in MEDIA_STATE:
            if key != "media_status" or status in {"done", "failed"}:
                row[key] = previous[key]
    return row


def project_message(message, spec, settings, protected, fetched_at):
    text = message.message or ""
    urls = [e.url for e in (message.entities or []) if isinstance(e, types.MessageEntityTextUrl)]
    text, redactions = redact("\n".join([text, *dict.fromkeys(urls)]))
    sender = message.sender  # Only cached entities from the history response.
    user = None
    if isinstance(sender, types.User):
        user = {
            "user_id": sender.id,
            "display_name": " ".join(n for n in (sender.first_name, sender.last_name) if n),
            "username": sender.username,
            "phone": sender.phone,
            "updated_at": fetched_at,
        }
    kind, size = classify_media(message)
    file = message.file
    name = getattr(file, "name", None) if kind not in {"photo", "voice"} else None
    mime = getattr(file, "mime_type", None)
    attachment = message.document or message.photo
    reply = message.reply_to
    row = {
        "chat_id": spec.chat_id,
        "msg_id": message.id,
        "topic_id": (reply.reply_to_top_id or reply.reply_to_msg_id)
        if reply and getattr(reply, "forum_topic", False)
        else None,
        "sent_at": message.date,
        "edited_at": message.edit_date,
        "sender_id": user["user_id"] if user else None,
        "reply_to_id": reply.reply_to_msg_id if reply else None,
        "text": text,
        "redactions": redactions,
        "media_kind": kind,
        "media_name": name,
        "media_ext": extension(name, mime, kind),
        "media_mime": mime,
        "media_size": size,
        "media_file_id": getattr(attachment, "id", None),
        "media_status": eligibility(kind, size, spec, settings, protected) if kind else None,
        "media_error": None,
        "media_path": None,
        "media_sha256": None,
        "media_attempts": 0,
        "fetched_at": fetched_at,
    }
    return row, user


def message_link(chat_id, kind, username, msg_id, topic_id=None):
    if kind in {"group", "private", "bot"}:
        return None
    if kind == "forum" and topic_id is not None:
        return f"https://t.me/c/{-chat_id - 10**12}/{topic_id}/{msg_id}"
    if username:
        return f"https://t.me/{username}/{msg_id}"
    return f"https://t.me/c/{-chat_id - 10**12}/{msg_id}"
