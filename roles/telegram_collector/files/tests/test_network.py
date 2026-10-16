import asyncio
from types import SimpleNamespace

import pytest

from tg_archive import cli, network
from tg_archive.config import ConfigError
from tg_archive.network import TelegramNetworkError, connect_client, parse_proxy
from tg_archive.telegram import make_client


def test_http_proxy_is_read_from_windows_not_environment(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(
        network.urllib.request,
        "getproxies_registry",
        lambda: {"https": "http://127.0.0.1:10809"},
        raising=False,
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.invalid:1")
    proxy = network.system_proxy()
    assert proxy["proxy_type"] == "http"
    assert (proxy["addr"], proxy["port"]) == ("127.0.0.1", 10809)


def test_client_actually_receives_windows_proxy(settings, monkeypatch):
    proxy = parse_proxy("http://127.0.0.1:10809")
    monkeypatch.setattr("tg_archive.telegram.system_proxy", lambda: proxy)
    settings = SimpleNamespace(require_credentials=lambda: None, api_id=1, api_hash="0" * 32)
    client = make_client(settings, "")
    assert client._proxy == proxy
    assert not client.is_connected()


@pytest.mark.parametrize(
    "url",
    ["https://localhost:8080", "http://", "http://localhost:99999", "http://localhost:8080/path"],
)
def test_bad_proxy_fails_without_echoing_url(url):
    with pytest.raises(ConfigError) as error:
        parse_proxy(url)
    assert url not in str(error.value)


def test_credentials_in_proxy_are_decoded_but_not_in_error():
    proxy = parse_proxy("http://SYNTHETIC%20USER:SYNTHETIC%20SECRET@127.0.0.1:10809")
    assert proxy["username"] == "SYNTHETIC USER"
    assert proxy["password"] == "SYNTHETIC SECRET"


def test_connect_errors_are_network_errors_and_hide_raw_details():
    async def fail():
        raise OSError(10061, "SYNTHETIC_SECRET in exception")

    with pytest.raises(TelegramNetworkError) as error:
        asyncio.run(connect_client(SimpleNamespace(connect=fail)))
    assert "during connect" in str(error.value)
    assert "10061" in str(error.value)
    assert "SYNTHETIC_SECRET" not in str(error.value)


def test_connect_has_bounded_timeout(monkeypatch):
    async def wait():
        await asyncio.sleep(10)

    monkeypatch.setattr(network, "CONNECT_TIMEOUT", 0.01)
    with pytest.raises(TelegramNetworkError, match="timed out"):
        asyncio.run(connect_client(SimpleNamespace(connect=wait)))


def test_login_connect_failure_is_not_reported_as_local_file_error(project, monkeypatch, capsys):
    (project / "sync.env").write_text("TG_API_ID=1\nTG_API_HASH=" + "0" * 32, encoding="utf-8")

    async def fail():
        raise OSError(10061, "SYNTHETIC_SECRET")

    async def disconnect():
        pass

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        cli, "make_client", lambda *args: SimpleNamespace(connect=fail, disconnect=disconnect)
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda *args: pytest.fail("Must not prompt"))
    assert cli.main(["--config", str(project / "tg-archive.toml"), "login"]) == 1
    output = capsys.readouterr()
    assert "network failure during connect" in output.err
    assert "Local file/configuration error" not in output.err
    assert "SYNTHETIC_SECRET" not in output.err
