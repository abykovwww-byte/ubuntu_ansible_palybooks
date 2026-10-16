"""Fail-closed MTProto gate, including direct and exported sender calls.

Internal sender creation is audited against Telethon 1.45.0 and uv.lock.
The gate does not turn the Telegram account itself into a read-only account.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

from telethon import TelegramClient, functions, utils, version
from telethon.network import MTProtoSender
from telethon.sessions import StringSession
from telethon.tl.alltlobjects import LAYER

from .config import ConfigError, Settings
from .network import system_proxy

_login = ContextVar("tg_archive_login", default=False)
_download = ContextVar("tg_archive_download", default=False)

READ_REQUESTS = frozenset(
    {
        functions.messages.GetHistoryRequest,
        functions.messages.GetMessagesRequest,
        functions.channels.GetMessagesRequest,
        functions.messages.GetDialogsRequest,
        functions.messages.GetPeerDialogsRequest,
        functions.channels.GetFullChannelRequest,
        functions.messages.GetFullChatRequest,
        functions.users.GetUsersRequest,
        functions.messages.SearchRequest,
    }
)
SERVICE_REQUESTS = frozenset(
    {
        functions.help.GetConfigRequest,
        functions.help.GetCdnConfigRequest,
        functions.updates.GetStateRequest,
        functions.PingRequest,
        functions.PingDelayDisconnectRequest,
    }
)
LOGIN_REQUESTS = frozenset(
    {
        functions.auth.SendCodeRequest,
        functions.auth.ResendCodeRequest,
        functions.auth.SignInRequest,
        functions.auth.CheckPasswordRequest,
        functions.account.GetPasswordRequest,
    }
)
DOWNLOAD_REQUESTS = frozenset(
    {
        functions.upload.GetFileRequest,
        functions.upload.GetCdnFileRequest,
        functions.upload.GetCdnFileHashesRequest,
        functions.upload.ReuploadCdnFileRequest,
        functions.auth.ExportAuthorizationRequest,
        functions.auth.ImportAuthorizationRequest,
    }
)
WRAPPERS = frozenset(
    {
        functions.InvokeWithLayerRequest,
        functions.InitConnectionRequest,
        functions.InvokeWithoutUpdatesRequest,
    }
)


class RequestDenied(RuntimeError):
    def __init__(self, request_type: str):
        self.request_type = request_type
        super().__init__("MTProto request denied: " + request_type)


def request_name(request) -> str:
    cls = type(request)
    return cls.__module__.removeprefix("telethon.tl.functions.") + "." + cls.__name__


@contextmanager
def login_scope():
    token = _login.set(True)
    try:
        yield
    finally:
        _login.reset(token)


@contextmanager
def download_scope():
    token = _download.set(True)
    try:
        yield
    finally:
        _download.reset(token)


class RequestPolicy:
    def __init__(self, journal: Callable[[str], None] | None = None):
        self.journal = journal

    def check(self, request, *, journal: bool = False, depth: int = 0) -> None:
        if depth > 8:
            raise RequestDenied("wrapper_depth")
        if isinstance(request, (list, tuple)):
            # Validate the whole batch before the sender can enqueue any member.
            for item in request:
                self.check(item, journal=journal, depth=depth + 1)
            return
        cls = type(request)
        name = request_name(request)
        if cls in WRAPPERS:
            self.check(request.query, journal=journal, depth=depth + 1)
            return
        allowed = (
            cls in READ_REQUESTS
            or cls in SERVICE_REQUESTS
            or (_login.get() and cls in LOGIN_REQUESTS)
            or (_download.get() and cls in DOWNLOAD_REQUESTS)
        )
        if journal and self.journal:
            self.journal(("allowed " if allowed else "denied ") + name)
        if not allowed:
            raise RequestDenied(name)

    def guard_sender(self, sender) -> None:
        if getattr(sender, "_tg_archive_guarded", False):
            return
        original = sender.send

        def guarded_send(request, ordered=False):
            self.check(request, journal=True)
            return original(request, ordered=ordered)

        sender.send = guarded_send
        sender._tg_archive_guarded = True


class ReadOnlyTelegramClient(TelegramClient):
    def __init__(self, session, api_id, api_hash, *, policy=None, **kwargs):
        if version.__version__ != "1.45.0":
            raise ConfigError("Telethon version differs from audited 1.45.0")
        if not isinstance(session, StringSession):
            raise ConfigError("Only an in-memory StringSession is supported")
        self.policy = policy or RequestPolicy()
        kwargs.update(
            receive_updates=False,
            catch_up=False,
            device_model="tg-archive (read-only)",
            flood_sleep_threshold=120,
        )
        super().__init__(session, api_id, api_hash, **kwargs)
        self.policy.guard_sender(self._sender)

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        # Before resolve(): a rejected request must not cause lookup requests either.
        self.policy.check(request)
        self.policy.guard_sender(sender)
        return await super()._call(
            sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
        )

    async def _on_login(self, user):
        # Telethon's default also fetches updates.GetDifference (all dialogs).
        # No update state is needed by a receive_updates=False archive client.
        self._mb_entity_cache.set_self_user(user.id, user.bot, user.access_hash)
        self._authorized = True
        return user

    async def _create_exported_sender(self, dc_id):
        # Adapted from the pinned Telethon method; guard BEFORE the auth import.
        if not _download.get():
            raise RequestDenied("auth.ExportAuthorizationRequest")
        dc = await self._get_dc(dc_id)
        sender = MTProtoSender(None, loggers=self._log)
        self.policy.guard_sender(sender)
        try:
            await sender.connect(
                self._connection(
                    dc.ip_address,
                    dc.port,
                    dc.id,
                    loggers=self._log,
                    proxy=self._proxy,
                    local_addr=self._local_addr,
                )
            )
            auth = await self(functions.auth.ExportAuthorizationRequest(dc_id))
            init = copy.copy(self._init_request)
            init.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            await sender.send(functions.InvokeWithLayerRequest(LAYER, init))
            return sender
        except BaseException:
            await sender.disconnect()
            raise

    async def _get_cdn_client(self, cdn_redirect):
        if not _download.get():
            raise RequestDenied("upload.GetCdnFileRequest")
        session = self._exported_sessions.get(cdn_redirect.dc_id)
        if not session:
            dc = await self._get_dc(cdn_redirect.dc_id, cdn=True)
            session = await utils.maybe_async(self.session.clone())
            await utils.maybe_async(session.set_dc(dc.id, dc.ip_address, dc.port))
            self._exported_sessions[cdn_redirect.dc_id] = session
        client = self.__class__(
            session,
            self.api_id,
            self.api_hash,
            policy=self.policy,
            proxy=self._proxy,
            timeout=self._timeout,
            loop=self.loop,
        )
        session.auth_key = self._sender.auth_key
        await client._sender.connect(
            self._connection(
                session.server_address,
                session.port,
                session.dc_id,
                loggers=self._log,
                proxy=self._proxy,
                local_addr=self._local_addr,
            )
        )
        return client


def make_client(settings: Settings, session: str, journal=None) -> ReadOnlyTelegramClient:
    settings.require_credentials()
    logger = logging.getLogger("tg_archive.telethon_silent")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)
    return ReadOnlyTelegramClient(
        StringSession(session),
        settings.api_id,
        settings.api_hash,
        policy=RequestPolicy(journal),
        base_logger=logger,
        proxy=system_proxy(),
    )
