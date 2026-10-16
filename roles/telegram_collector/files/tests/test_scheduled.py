from datetime import date
from types import SimpleNamespace

import pytest

from tg_archive.config import ChatSpec
from tg_archive.scheduled import collector_command, safe_report


def test_work_hours_collector_is_scoped_and_does_not_download_media():
    selected = ChatSpec(123456789, "chat-123456789", date(2025, 1, 1))
    other = ChatSpec(-7, "synthetic", date(2025, 1, 1))
    command, specs = collector_command(
        SimpleNamespace(chats=[selected, other]),
        SimpleNamespace(chat=selected.slug, no_media=True),
    )
    assert command[1:] == [
        "-m", "tg_archive.cli", "run", "--chat", "chat-123456789", "--no-media"
    ]
    assert specs == [selected]


def test_unknown_scheduled_chat_is_rejected_before_starting_collector():
    with pytest.raises(ValueError, match="already be configured"):
        collector_command(
            SimpleNamespace(chats=[]), SimpleNamespace(chat="unknown", no_media=True)
        )


def test_daily_collector_preserves_existing_full_collection():
    settings = SimpleNamespace(chats=[ChatSpec(-7, "synthetic", date(2025, 1, 1))])
    command, specs = collector_command(settings, SimpleNamespace(chat=None, no_media=False))
    assert command[1:] == ["-m", "tg_archive.cli", "run"]
    assert specs == settings.chats


def test_daily_log_filters_everything_except_known_counters_and_slugs():
    spec = ChatSpec(-7, "synthetic", date(2025, 1, 1))
    private = "SYNTHETIC_PRIVATE_VALUE"
    report = safe_report(
        {
            "status": "ok",
            "text": private,
            "filename": private,
            "counts": {
                "-7": {
                    "fetched": 5,
                    "phone": private,
                    "sender_id": 9,
                    "text": private,
                    "media_bytes": private,
                    "status": "ok",
                },
                "media": {"fsck_status": "ok", "media_quota": 0, "file_name": private},
            },
        },
        [spec],
    )
    assert report == {
        "status": "ok",
        "counts": {"synthetic": {"fetched": 5, "status": "ok"}},
        "media": {"media_quota": 0, "fsck_status": "ok"},
    }
    assert private not in str(report) and "sender_id" not in str(report)
