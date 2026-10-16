"""All profile/message examples in this file are synthetic."""

from datetime import UTC, date, datetime

import pytest
from telethon import types

from tg_archive.config import ChatSpec
from tg_archive.content import extension, message_link, project_message, redact, transition

STAMP = datetime(2026, 10, 12, tzinfo=UTC)
SPEC = ChatSpec(-7, "synthetic", date(2025, 1, 1))


def message(msg_id=1, text="Synthetic test", media=None):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChat(7),
        date=STAMP,
        message=text,
        media=media,
        from_id=types.PeerUser(9),
    )


def document(file_id=31, name="schema.VSDX", size=25, mime="application/octet-stream"):
    return types.MessageMediaDocument(
        document=types.Document(
            id=file_id,
            access_hash=9999,
            file_reference=b"synthetic",
            date=STAMP,
            mime_type=mime,
            size=size,
            dc_id=2,
            attributes=[types.DocumentAttributeFilename(name)],
        )
    )


@pytest.mark.parametrize(
    "text,kind",
    [
        (
            "-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC\n-----END RSA PRIVATE KEY-----",
            "private_key",
        ),
        ("-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----", "private_key"),
        ("Authorization: Bearer synthetic-value", "bearer"),
        ("eyJzeW50aGV0aWM.e30.c3ludGhldGlj", "jwt"),
        ("glpat-synthetic_value", "gitlab_token"),
        ("AKIA" + "A" * 16, "aws_key"),
        ("123456:" + "X" * 35, "telegram_bot"),
        ("пароль: SYNTHETIC", "credential"),
        ("Password = SYNTHETIC", "credential"),
        ("passwd=SYNTHETIC", "credential"),
        ("pwd:SYNTHETIC", "credential"),
        ("token: SYNTHETIC", "credential"),
        ("secret=SYNTHETIC", "credential"),
        ("api_key=SYNTHETIC", "credential"),
        ("api-key:SYNTHETIC", "credential"),
    ],
)
def test_masks(text, kind):
    assert redact(text) == (f"[REDACTED:{kind}]", 1)


def test_ordinary_password_word_and_identifiers_remain():
    text = "Пароль нужно сменить. SOC-1234 10.1.2.3 srv-db-01 /var/test schema.vsdx"
    assert redact(text) == (text, 0)
    assert redact("token: AKIA" + "A" * 16)[1] == 1


def test_hidden_urls_are_searchable_and_masked(settings):
    msg = message(text="Synthetic link")
    msg.entities = [
        types.MessageEntityTextUrl(0, 9, "https://wiki.invalid/SOC-1234"),
        types.MessageEntityTextUrl(0, 9, "https://wiki.invalid/?token=SYNTHETIC"),
    ]
    row, _ = project_message(msg, SPEC, settings, False, STAMP)
    assert "https://wiki.invalid/SOC-1234" in row["text"]
    assert "SYNTHETIC" not in row["text"] and row["redactions"] == 1


def test_user_projection_contains_only_allowed_fields(settings):
    msg = message()
    msg._sender = types.User(
        id=9,
        first_name="Synthetic",
        last_name="User",
        username="synthetic_user",
        phone="00000000000",
        access_hash=999,
        status=types.UserStatusEmpty(),
    )
    row, user = project_message(msg, SPEC, settings, False, STAMP)
    assert set(user) == {"user_id", "display_name", "username", "phone", "updated_at"}
    assert row["sender_id"] == 9 and "access_hash" not in row
    msg._sender = types.Channel(id=12, title="Synthetic", photo=types.ChatPhotoEmpty(), date=STAMP)
    row, user = project_message(msg, SPEC, settings, False, STAMP)
    assert row["sender_id"] is None and user is None


@pytest.mark.parametrize(
    "name,mime,kind,expected",
    [
        ("schema.VSDX", None, "document", "vsdx"),
        (None, None, "photo", "jpg"),
        (None, None, "voice", "ogg"),
        (None, None, "video_note", "mp4"),
        ("noextension", "application/pdf", "document", "pdf"),
        ("noextension", "application/x-unknown", "document", None),
        ("x." + "y" * 17, None, "document", None),
    ],
)
def test_fixed_extension_table(name, mime, kind, expected):
    assert extension(name, mime, kind) == expected


@pytest.mark.parametrize(
    "protected,media,kind,size,status",
    [
        (True, False, "document", 2**40, "skipped_protected"),
        (False, False, "document", 2**40, "skipped_disabled"),
        (False, True, "sticker", 2**40, "skipped_kind"),
        (False, True, "document", 2**40, "skipped_too_big"),
        (False, True, "document", 25, "pending"),
    ],
)
def test_metadata_and_eligibility_priority(settings, protected, media, kind, size, status):
    from tg_archive.content import eligibility

    spec = ChatSpec(-7, "synthetic", SPEC.since, media=media)
    assert eligibility(kind, size, spec, settings, protected) == status
    row, _ = project_message(message(media=document(size=2**40)), SPEC, settings, False, STAMP)
    assert row["media_name"] == "schema.VSDX" and row["media_ext"] == "vsdx"
    assert row["media_status"] == "skipped_too_big"


@pytest.mark.parametrize("status", ["done", "failed", "pending", "skipped_disabled"])
def test_same_file_preserves_transfer_state(settings, status):
    row, _ = project_message(message(media=document()), SPEC, settings, False, STAMP)
    previous = dict(
        row,
        media_status=status,
        media_path="synthetic/file",
        media_sha256="synthetic-sha",
        media_error="timeout",
        media_attempts=3,
    )
    result = transition(row.copy(), previous)
    assert result["media_path"] == "synthetic/file" and result["media_attempts"] == 3
    assert result["media_status"] == (status if status in {"done", "failed"} else "pending")


def test_new_file_resets_old_failures_and_removed_file_nulls_all(settings):
    old, _ = project_message(message(media=document()), SPEC, settings, False, STAMP)
    old.update(
        media_status="failed",
        media_attempts=3,
        media_path="synthetic/old",
        media_sha256="synthetic-sha",
        media_error="timeout",
    )
    new, _ = project_message(message(media=document(file_id=32)), SPEC, settings, False, STAMP)
    new = transition(new, old)
    assert new["media_status"] == "pending" and new["media_attempts"] == 0
    assert new["media_path"] is None and new["media_error"] is None
    removed, _ = project_message(message(), SPEC, settings, False, STAMP)
    removed = transition(removed, old)
    assert all(
        value is None
        for key, value in removed.items()
        if key.startswith("media_") and key != "media_attempts"
    )
    assert removed["media_attempts"] == 0


def test_non_downloadable_preview_keeps_metadata(settings):
    msg = message(media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=5)))
    row, _ = project_message(msg, SPEC, settings, False, STAMP)
    assert row["media_kind"] == "other" and row["media_status"] == "skipped_kind"


@pytest.mark.parametrize(
    "chat,kind,username,topic,expected",
    [
        (-7, "group", None, None, None),
        (-1000000000007, "channel", "synthetic", None, "https://t.me/synthetic/8"),
        (-1000000000007, "supergroup", None, None, "https://t.me/c/7/8"),
        (-1000000000007, "forum", None, 3, "https://t.me/c/7/3/8"),
    ],
)
def test_message_links(chat, kind, username, topic, expected):
    assert message_link(chat, kind, username, 8, topic) == expected
