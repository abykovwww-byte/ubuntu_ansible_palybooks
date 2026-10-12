import asyncio
import socket
import time

import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied, Scope, digest
from security_lab.target_resolution import ResolutionSnapshot, TargetResolver

NOW = 1_900_000_000.0


def scope(hosts=('example.test',), reference='operator:offline-test'):
    return Scope.model_validate({
        'targets': [{'host': host, 'port': 8443, 'paths': ['/public'], 'methods': ['GET']} for host in hosts],
        'authorization': {'reference': reference, 'owner': 'fixture', 'basis': 'Injected-resolver tests; no network',
                          'hosts': list(hosts), 'valid_from': int(NOW) - 1, 'valid_until': int(NOW) + 100},
        'expires_at': int(NOW) + 100})


def resolved(answers, selected=None, **options):
    async def resolver(host, port):
        return answers
    configured = TargetResolver(resolver, clock=lambda: NOW, **options)
    return asyncio.run(configured.resolve(selected or scope()))


def test_complete_canonical_snapshot_scope_hash_and_copied_policy_pins(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected live DNS')))
    selected = scope()
    snapshot = resolved(['8.8.8.8', '1.1.1.1', '8.8.8.8'], selected)
    assert snapshot.scope_hash == digest(selected.model_dump(mode='json'))
    assert snapshot.upstreams[0].addresses == ('1.1.1.1', '8.8.8.8')
    assert snapshot.ttl_seconds is None
    assert ResolutionSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    pins = snapshot.policy_upstreams()
    pins[0].addresses.append('8.8.4.4')
    assert snapshot.upstreams[0].addresses == ('1.1.1.1', '8.8.8.8')
    with pytest.raises(ValidationError):
        snapshot.resolved_at = NOW + 1


@pytest.mark.parametrize('answers', [
    [], ['8.8.8.8', '127.0.0.1'], ['8.8.8.8', '10.0.0.1'], ['169.254.169.254'], ['::1'], ['fe80::1'],
    ['0.0.0.0'], ['224.0.0.1'], ['ff02::1'], ['192.0.2.1'], ['100.64.0.1'],
    ['example.test'], ['8.8.8.0/24'], ['2001:4860:4860::8888%eth0'], [123], ['8.8.8.8'] * 17,
    '8.8.8.8'])
def test_one_invalid_or_nonpublic_answer_denies_complete_snapshot(answers):
    with pytest.raises(Denied):
        resolved(answers)


def test_ipv6_public_answer_is_literal_and_complete():
    snapshot = resolved(['2001:4860:4860::8888'])
    assert snapshot.upstreams[0].addresses == ('2001:4860:4860::8888',)


def test_lab_private_exception_requires_exact_host_reference_and_provided_ip():
    lab = scope(('fixture.example.test',), 'lab:isolated-docker-fixture')
    assert resolved(['172.30.62.3'], lab, lab_private_addresses=('172.30.62.3',)).upstreams[0].addresses == ('172.30.62.3',)
    for selected in (scope(), scope(('fixture.example.test',)), scope(('example.test',), 'lab:isolated-docker-fixture')):
        with pytest.raises(Denied):
            resolved(['172.30.62.3'], selected, lab_private_addresses=('172.30.62.3',))
    with pytest.raises(Denied):
        resolved(['172.30.62.4'], lab, lab_private_addresses=('172.30.62.3',))
    for address in ('127.0.0.1', '169.254.169.254', '0.0.0.0', '::1', '8.8.8.8'):
        with pytest.raises(ValueError):
            TargetResolver(lambda host, port: None, lab_private_addresses=(address,))


def test_revalidation_accepts_reordering_and_dedup_but_rejects_complete_set_changes():
    async def scenario():
        answers = ['8.8.8.8', '1.1.1.1']
        clock = [NOW]
        async def resolver(host, port):
            return list(answers)
        configured = TargetResolver(resolver, clock=lambda: clock[0])
        selected = scope()
        saved = await configured.resolve(selected)
        answers[:] = ['1.1.1.1', '8.8.8.8', '1.1.1.1']
        clock[0] += 1
        fresh = await configured.revalidate(selected, saved)
        assert fresh.upstreams == saved.upstreams and fresh.resolved_at == NOW + 1
        assert fresh.snapshot_hash != saved.snapshot_hash
        for changed in (['8.8.8.8'], ['8.8.8.8', '8.8.4.4'], ['8.8.8.8', '1.1.1.1', '8.8.4.4'], []):
            answers[:] = changed
            with pytest.raises(Denied):
                await configured.revalidate(selected, saved)
            assert saved.upstreams[0].addresses == ('1.1.1.1', '8.8.8.8')
    asyncio.run(scenario())


def test_scope_or_snapshot_tampering_denied_before_revalidation_lookup():
    async def scenario():
        calls = []
        async def resolver(host, port):
            calls.append((host, port))
            return ['8.8.8.8']
        configured = TargetResolver(resolver, clock=lambda: NOW)
        selected = scope()
        saved = await configured.resolve(selected)
        tampered = saved.model_copy(update={'snapshot_hash': '0' * 64})
        with pytest.raises(Denied, match='integrity'):
            tampered.policy_upstreams()
        with pytest.raises(Denied, match='integrity'):
            await configured.revalidate(selected, tampered)
        with pytest.raises(Denied, match='different scope'):
            await configured.revalidate(scope(('other.test',)), saved)
        assert calls == [('example.test', 8443)]
    asyncio.run(scenario())


def test_complete_scope_resolution_has_bounded_concurrency_and_no_partial_success():
    async def scenario():
        current, maximum = 0, 0
        calls = []
        fail = [False]
        async def resolver(host, port):
            nonlocal current, maximum
            current += 1
            maximum = max(current, maximum)
            try:
                calls.append((host, port))
                await asyncio.sleep(0.01)
                if fail[0] and host == 'b.example.test':
                    raise RuntimeError('password=private secret')
                return ['8.8.8.8']
            finally:
                current -= 1
        configured = TargetResolver(resolver, clock=lambda: NOW, max_concurrency=2)
        selected = scope(('c.example.test', 'b.example.test', 'a.example.test'))
        snapshot = await configured.resolve(selected)
        assert [item.host for item in snapshot.upstreams] == ['a.example.test', 'b.example.test', 'c.example.test']
        assert maximum == 2 and len(calls) == 3
        fail[0] = True
        with pytest.raises(Denied) as error:
            await configured.resolve(selected)
        assert str(error.value) == 'Target DNS lookup failed' and 'private secret' not in str(error.value)
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_uncooperative_timeout_returns_promptly_and_retains_bounded_slot_until_done():
    async def scenario():
        release = asyncio.Event()
        calls = 0
        async def resolver(host, port):
            nonlocal calls
            calls += 1
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A native resolver wrapper may not finish on Python cancel.
                await release.wait()
            return ['8.8.8.8']
        configured = TargetResolver(resolver, clock=lambda: NOW, timeout_seconds=0.03, max_concurrency=1)
        try:
            start = time.monotonic()
            with pytest.raises(Denied, match='timeout'):
                await configured.resolve(scope())
            assert time.monotonic() - start < 0.5
            with pytest.raises(Denied, match='timeout'):
                await configured.resolve(scope())
            assert calls == 1 and len(configured.pending) == 1
        finally:
            release.set()
            await asyncio.sleep(0.01)
        assert configured.pending == set()
    asyncio.run(scenario())


def test_authorization_expiry_or_scope_change_during_lookup_prevents_snapshot():
    async def scenario():
        selected = scope()
        clock = [NOW]
        async def expiring(host, port):
            clock[0] += 101
            return ['8.8.8.8']
        with pytest.raises(Denied):
            await TargetResolver(expiring, clock=lambda: clock[0]).resolve(selected)
        async def changing(host, port):
            selected.targets[0].paths.append('/admin')
            return ['8.8.8.8']
        with pytest.raises(Denied, match='Scope changed'):
            await TargetResolver(changing, clock=lambda: NOW).resolve(selected)
    asyncio.run(scenario())
