from datetime import UTC, date, datetime

import pytest

from tg_archive.config import ChatSpec
from tg_archive.paths import media_path, safe_name, within_root
from tg_archive.route import allowed_path, classify_path


@pytest.mark.parametrize(
    "name",
    [
        "..\\..\\x",
        "../x",
        "CON",
        "con.txt",
        "PRN.pdf",
        "AUX",
        "NUL",
        "COM1.log",
        "LPT9.txt",
        "a:b",
        "x?.txt",
        "../" + "файл" * 100 + ".vsdx",
    ],
)
def test_safe_filename(name):
    result = safe_name(name)
    assert len(result.encode("utf-8")) <= 200
    assert not any(c in result for c in '\\/:*?"<>|')
    assert result not in {".", ".."} and not result.endswith((".", " "))
    if name.endswith(".vsdx"):
        assert result.endswith(".vsdx")


def test_prefix_and_unicode_fit_full_filename_budget():
    path = media_path(
        ChatSpec(-7, "synthetic", date(2025, 1, 1)),
        dict(
            msg_id=999999999999,
            sent_at=datetime(2026, 10, 12, tzinfo=UTC),
            media_name="файл" * 100 + ".vsdx",
            media_ext="vsdx",
        ),
    )
    assert path.startswith("synthetic/2026-10/999999999999-")
    assert len(path.rsplit("/", 1)[1].encode("utf-8")) <= 200 and path.endswith(".vsdx")


@pytest.mark.parametrize("relative", ["../a", "/a", "x/../a", "x//a", "x\\a", "x/./a"])
def test_root_boundary(relative):
    with pytest.raises(ValueError):
        within_root("/media", relative)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("192.168.1.88:41641", "lan"),
        ("95.31.191.141:41641", "direct"),
        ("", "relay"),
        ("[2001:db8::1]:41641", "direct"),
    ],
)
def test_path_from_current_tailscale_endpoint(endpoint, expected):
    data = {
        "Peer": {
            "synthetic-peer": {
                "Online": True,
                "TailscaleIPs": ["100.117.52.16"],
                "CurAddr": endpoint,
            }
        }
    }
    assert classify_path(data, "100.117.52.16", "192.168.1.0/24") == expected
    assert classify_path(data, "100.1.2.3", "192.168.1.0/24") is None


@pytest.mark.parametrize(
    "policy,path,expected",
    [
        ("lan", "lan", True),
        ("lan", "direct", False),
        ("lan", "relay", False),
        ("direct", "lan", True),
        ("direct", "direct", True),
        ("direct", "relay", False),
        ("any", "relay", True),
        ("any", None, False),
    ],
)
def test_transfer_path_policy(policy, path, expected):
    assert allowed_path(policy, path) == expected
