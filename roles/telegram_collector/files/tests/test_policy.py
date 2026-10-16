import asyncio
from types import SimpleNamespace

import pytest
from telethon import functions, types
from telethon.sessions import StringSession

from tg_archive.telegram import (
    ReadOnlyTelegramClient,
    RequestDenied,
    RequestPolicy,
    download_scope,
    login_scope,
)


def actions():
    peer = types.InputPeerSelf()
    return [
        functions.messages.SendMessageRequest(peer=peer, message="SYNTHETIC", random_id=1),
        functions.messages.ReadHistoryRequest(peer=peer, max_id=1),
        functions.messages.SendReactionRequest(peer=peer, msg_id=1, reaction=[]),
        functions.channels.JoinChannelRequest(channel=types.InputChannel(7, 8)),
        functions.messages.EditMessageRequest(peer=peer, id=1, message="SYNTHETIC"),
        functions.messages.DeleteMessagesRequest(id=[1]),
        functions.messages.ImportChatInviteRequest(hash="SYNTHETIC"),
        functions.auth.LogOutRequest(),
    ]


@pytest.mark.parametrize("scope", [None, login_scope, download_scope])
@pytest.mark.parametrize("action", actions())
def test_write_actions_are_denied_even_in_privileged_scopes(action, scope):
    policy = RequestPolicy()
    if scope:
        with scope(), pytest.raises(RequestDenied):
            policy.check(action)
    else:
        with pytest.raises(RequestDenied):
            policy.check(action)


def test_wrapped_action_is_denied():
    request = functions.InvokeWithoutUpdatesRequest(
        functions.InvokeWithLayerRequest(1, actions()[0])
    )
    with pytest.raises(RequestDenied):
        RequestPolicy().check(request)


def test_guarded_sender_validates_entire_batch_before_enqueue():
    calls = []
    sender = SimpleNamespace(send=lambda request, ordered=False: calls.append(request))
    RequestPolicy().guard_sender(sender)
    with pytest.raises(RequestDenied):
        sender.send([functions.help.GetConfigRequest(), actions()[0]])
    assert calls == []
    sender.send(functions.help.GetConfigRequest())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "rpc",
    [
        functions.auth.ExportAuthorizationRequest(dc_id=2),
        functions.auth.ImportAuthorizationRequest(id=1, bytes=b"SYNTHETIC"),
        functions.upload.GetCdnFileRequest(file_token=b"SYNTHETIC", offset=0, limit=4096),
    ],
)
def test_cross_dc_auth_and_file_requests_require_download_scope(rpc):
    policy = RequestPolicy()
    with pytest.raises(RequestDenied):
        policy.check(rpc)
    with login_scope(), pytest.raises(RequestDenied):
        policy.check(rpc)
    with download_scope():
        policy.check(rpc)
    with pytest.raises(RequestDenied):
        policy.check(rpc)


def test_login_permissions_do_not_leak():
    request = functions.account.GetPasswordRequest()
    policy = RequestPolicy()
    with login_scope():
        policy.check(request)
    with pytest.raises(RequestDenied):
        policy.check(request)


def test_journal_contains_types_never_request_parameters():
    output = []
    policy = RequestPolicy(output.append)
    secret = "SYNTHETIC_PRIVATE_VALUE"
    with download_scope():
        policy.check(
            functions.auth.ImportAuthorizationRequest(id=123, bytes=secret.encode()), journal=True
        )
    with pytest.raises(RequestDenied):
        policy.check(
            functions.messages.SendMessageRequest(
                peer=types.InputPeerSelf(), message=secret, random_id=42
            ),
            journal=True,
        )
    assert secret not in "\n".join(output)
    assert "123" not in "\n".join(output)
    assert "allowed auth.ImportAuthorizationRequest" in output
    assert "denied messages.SendMessageRequest" in output


def test_unknown_request_is_denied():
    with pytest.raises(RequestDenied):
        RequestPolicy().check(functions.contacts.GetContactsRequest(hash=0))


def test_client_blocks_before_resolve_and_transport(monkeypatch):
    async def run():
        client = ReadOnlyTelegramClient(StringSession(), 1, "0" * 32)
        sent = []
        # Even a separate unguarded sender is checked before any request resolve.
        sender = SimpleNamespace(send=lambda *args, **kwargs: sent.append(args))
        with pytest.raises(RequestDenied):
            await client._call(sender, actions()[0])
        assert sent == []
        assert client._no_updates
        assert client.flood_sleep_threshold == 120
        with pytest.raises(RequestDenied):
            client._sender.send(actions()[0])
        assert not client.is_connected()

    asyncio.run(run())


def test_file_session_is_rejected_before_file_creation(tmp_path):
    target = tmp_path / "forbidden.session"
    with pytest.raises(ValueError):
        ReadOnlyTelegramClient(str(target), 1, "0" * 32)
    assert not target.exists()


def test_exported_sender_is_guarded_before_import(monkeypatch):
    import tg_archive.telegram as module

    senders = []

    class Sender:
        def __init__(self, *args, **kwargs):
            self.calls = []
            senders.append(self)

        async def connect(self, connection):
            pass

        async def disconnect(self):
            pass

        def send(self, request, ordered=False):
            self.calls.append(request)
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

    async def run():
        client = ReadOnlyTelegramClient(StringSession(), 1, "0" * 32)

        async def dc(_):
            return SimpleNamespace(ip_address="127.0.0.1", port=443, id=2)

        async def call(sender, request, **kwargs):
            client.policy.check(request)
            return SimpleNamespace(id=1, bytes=b"SYNTHETIC")

        monkeypatch.setattr(module, "MTProtoSender", Sender)
        monkeypatch.setattr(client, "_get_dc", dc)
        monkeypatch.setattr(client, "_call", call)
        with pytest.raises(RequestDenied):
            await client._create_exported_sender(2)
        assert not senders
        with download_scope():
            sender = await client._create_exported_sender(2)
        assert len(sender.calls) == 1
        with pytest.raises(RequestDenied):
            sender.send(functions.auth.ImportAuthorizationRequest(id=1, bytes=b"SYNTHETIC"))
        with pytest.raises(RequestDenied):
            sender.send(actions()[0])
        assert len(sender.calls) == 1

    asyncio.run(run())
