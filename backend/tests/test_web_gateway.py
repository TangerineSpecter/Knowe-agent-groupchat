"""Exercise real HTTP/WS boundaries; no provider or external network required."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from backend.web import COOKIE, WebConfig, create_app, persistent_secret
from backend.web_state import WebState
from backend.web_workspaces import project_directory


def config(tmp_path: Path, **changes):
    return WebConfig(password='a-long-test-password', secret='a' * 64,
                     workspace=tmp_path / 'workspace', data=tmp_path / 'data',
                     dist=tmp_path / 'dist', runtime_token='b' * 64, **changes)


async def login(client):
    origin = str(client.make_url('/')).rstrip('/')
    response = await client.post('/api/web/login', json={'password': 'a-long-test-password'}, headers={'Origin': origin})
    assert response.status == 200, await response.text()
    assert 'HttpOnly' in response.headers['Set-Cookie']
    return origin


@pytest.mark.asyncio
async def test_auth_origin_logout_and_no_runtime_secret(tmp_path):
    cfg = config(tmp_path)
    async with TestClient(TestServer(create_app(cfg))) as client:
        assert (await client.get('/api/runtime/settings')).status == 401
        assert (await client.post('/api/web/login', json={'password': cfg.password})).status == 403
        origin = str(client.make_url('/')).rstrip('/')
        bad = await client.post('/api/web/login', json={'password': 'bad'}, headers={'Origin': origin})
        assert bad.status == 401
        await login(client)
        session = await (await client.get('/api/web/session')).json()
        assert session['authenticated'] is True
        assert cfg.runtime_token not in str(session)
        cookie = client.session.cookie_jar.filter_cookies(client.make_url('/'))[COOKIE].value
        assert (await client.post('/api/web/logout', headers={'Origin': 'https://evil.invalid'})).status == 403
        assert (await client.post('/api/web/logout', headers={'Origin': origin})).status == 200
        # Captured cookies are revoked, not just removed from one browser.
        result = await client.get('/api/web/notifications', headers={'Cookie': f'{COOKIE}={cookie}'})
        assert result.status == 401


@pytest.mark.asyncio
async def test_directory_traversal_upload_and_signed_download(tmp_path, monkeypatch):
    from backend.config import CONFIG
    object.__setattr__(CONFIG, 'attachment_key', 'c' * 64)
    cfg = config(tmp_path)
    cfg.workspace.mkdir()
    (cfg.workspace / '项目资料').mkdir()
    (cfg.workspace / 'escape').symlink_to(tmp_path)
    async with TestClient(TestServer(create_app(cfg))) as client:
        origin = await login(client)
        listing = await (await client.get('/api/web/workspaces')).json()
        assert '项目资料' in listing['directories']
        assert 'escape' not in listing['directories']
        assert (await client.get('/api/web/workspaces', params={'path': '../'})).status == 400
        assert (await client.get('/api/web/workspaces', params={'path': 'escape'})).status == 400
        form = aiohttp.FormData()
        form.add_field('files', b'hello world', filename='测试.txt', content_type='text/plain')
        result = await client.post('/api/web/uploads', data=form, headers={'Origin': origin})
        assert result.status == 200, await result.text()
        file = (await result.json())['files'][0]
        assert Path(file['path']).read_bytes() == b'hello world'
        downloaded = await client.get('/api/web/attachments', params={'path': file['path'], 'sig': file['sig']})
        assert downloaded.status == 200
        assert await downloaded.read() == b'hello world'
        assert 'attachment' in downloaded.headers['Content-Disposition']
        assert (await client.get('/api/web/attachments', params={'path': file['path'], 'sig': 'fake'})).status == 403
        from backend.file_library import FileLibrary
        library = FileLibrary(cfg.data)
        entry = library.listing()['items'][0]
        download = await client.get('/api/web/library/download', params={'id': entry['id']})
        assert download.status == 200 and await download.read() == b'hello world'
        assert 'filename*=' in download.headers['Content-Disposition']
        assert (await client.post('/api/web/uploads/discard', json={'path': file['path'], 'sig': 'bad'}, headers={'Origin': origin})).status == 403
        removed = await client.post('/api/web/uploads/discard', json=file, headers={'Origin': origin})
        assert removed.status == 200 and (await removed.json())['removed'] is True
        assert not Path(file['path']).exists()


@pytest.mark.asyncio
async def test_proxy_http_and_ws_use_server_credentials(tmp_path):
    token = 'b' * 64
    messages = []
    async def http(request):
        assert request.headers['X-Knowe-Runtime-Token'] == token
        assert 'token' not in request.query and '_kpt' not in request.query
        return web.Response(text='<h1>preview</h1>', content_type='text/html')
    async def ws(request):
        assert request.headers['X-Knowe-Runtime-Token'] == token
        result = web.WebSocketResponse()
        await result.prepare(request)
        async for message in result:
            messages.append(message.data)
            await result.send_str(message.data)
        return result
    upstream_app = web.Application()
    upstream_app.router.add_get('/preview', http)
    upstream_app.router.add_get('/ws', ws)
    async with TestServer(upstream_app) as upstream:
        cfg = config(tmp_path, http_base=str(upstream.make_url('')).rstrip('/'),
                     ws_url=str(upstream.make_url('/ws')).replace('http:', 'ws:'))
        async with TestClient(TestServer(create_app(cfg))) as client:
            origin = await login(client)
            result = await client.get('/api/runtime/preview?token=bad&_kpt=bad')
            assert result.status == 200
            assert result.headers['Content-Security-Policy'].startswith('sandbox')
            assert (await client.post('/api/runtime/shutdown', headers={'Origin': origin})).status == 404
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect('/api/runtime/ws', origin='https://evil.invalid')
            connection = await client.ws_connect('/api/runtime/ws', origin=origin)
            await connection.send_json({'type': 'ping'})
            assert (await connection.receive(timeout=2)).json()['type'] == 'ping'
            await connection.send_json({'type': 'shutdown'})
            await connection.send_bytes(b'{"type":"shutdown"}')
            await connection.send_json({'type': 'ping'})
            await connection.receive(timeout=2)
            await connection.close()
            assert len(messages) == 2


def test_web_directory_default_and_desktop_policy(tmp_path, monkeypatch):
    monkeypatch.delenv('KNOWE_WEB_WORKSPACE_ROOT', raising=False)
    assert project_directory('project_20261112000001', None) is None
    root = tmp_path / 'workspace'
    monkeypatch.setenv('KNOWE_WEB_WORKSPACE_ROOT', str(root))
    path = project_directory('project_20261112000001', None)
    assert Path(path).is_dir()
    assert Path(path).is_relative_to(root)
    assert project_directory('project_20261112000001', None) == path
    with pytest.raises(ValueError):
        project_directory('project_20261112000001', str(tmp_path))
    (root / 'outside').symlink_to(tmp_path)
    with pytest.raises(ValueError):
        project_directory('project_20261112000001', str(root / 'outside'))


@pytest.mark.asyncio
async def test_notifications_persist_without_clients_and_ack_does_not_swallow_new(tmp_path):
    from backend.hub import Hub
    state = WebState(tmp_path)
    hub = Hub()
    async def observe(event):
        await asyncio.to_thread(state.record, event)
    hub.event_listeners.append(observe)
    await hub.emit('project_20261112000001', {'type': 'approval_card', 'tool': 'dispatch', 'card_id': 'card-1', 'card': {}})
    first = state.list()
    assert first['unread'] == 1 and first['pending'] == 1
    await hub.emit('project_20261112000001', {'type': 'error', 'message': 'failed'})
    state.mark_read(through=first['latest_id'])
    assert WebState(tmp_path).list()['unread'] == 1
    # Replaying the same event after restart cannot generate duplicate notifications.
    state.record(hub.projects['project_20261112000001'].ring.events()[0])
    assert len(state.list()['items']) == 2
    await hub.emit('project_20261112000001', {'type': 'approval_resolved', 'card_id': 'card-1', 'resolution': 'approved'})
    assert state.list()['pending'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('next_event', ['agent_thinking', 'state_snapshot'])
async def test_slow_notification_preserves_project_broadcast_order(tmp_path, next_event):
    from backend.hub import Hub

    state = WebState(tmp_path)
    hub = Hub()
    projection_started = asyncio.Event()
    release_projection = asyncio.Event()
    received = []

    class Sink:
        async def send(self, event):
            received.append((event['project_id'], event['seq']))
            return True

    async def observe(event):
        if event.get('type') == 'error':
            projection_started.set()
            await release_projection.wait()
            await asyncio.to_thread(state.record, event)

    hub.add_client(Sink())
    hub.event_listeners.append(observe)
    await hub.emit('demo', {'type': 'agent_thinking', 'agent_id': 'worker'})
    first = asyncio.create_task(hub.emit('demo', {'type': 'error', 'message': 'failed'}))
    second_started = asyncio.Event()

    async def send_next():
        second_started.set()
        if next_event == 'state_snapshot':
            return await hub.snapshot('demo')
        return await hub.emit('demo', {'type': 'agent_thinking', 'agent_id': 'worker'})

    second = None
    try:
        await asyncio.wait_for(projection_started.wait(), 2)
        second = asyncio.create_task(send_next())
        await asyncio.wait_for(second_started.wait(), 2)
        # A slow projection in one project must not block another project's traffic.
        await asyncio.wait_for(hub.emit('other', {'type': 'agent_thinking', 'agent_id': 'worker'}), 2)
        assert ('other', 1) in received
        assert ('demo', 3) not in received
    finally:
        release_projection.set()
        await asyncio.gather(first, *([second] if second is not None else []))
    assert [seq for pid, seq in received if pid == 'demo'] == [1, 2, 3]
    assert state.list()['unread'] == 1


def test_secrets_survive_restart_and_expired_or_tampered_sessions_fail(tmp_path):
    assert persistent_secret(tmp_path, 'session.key') == persistent_secret(tmp_path, 'session.key')
    cfg = config(tmp_path)
    session = cfg.session()
    assert cfg.authenticated(session)
    assert not cfg.authenticated(session + 'tampered')
    assert not cfg.authenticated('0.nonce.signature')
    assert not replace(cfg, secret='x' * 64).authenticated(session)


def test_model_pool_public_snapshot_and_redacted_roundtrip_preserve_separate_keys():
    from backend import runtime_settings as settings
    def binding(model, key):
        return {'provider': 'custom', 'model': model, 'api_key': key,
                'base_url': 'https://models.example.invalid', 'transport': 'openai_chat'}
    settings.apply({'model_profiles': [binding('one', 'secret-one'), binding('two', 'secret-two')],
                    'main_pool': [0], 'aux_pool': [1]})
    public = settings.api_snapshot()
    assert 'secret-one' not in str(public) and 'secret-two' not in str(public)
    assert public['model_profiles'][0]['has_api_key'] is True
    settings.apply({'model_profiles': list(reversed(public['model_profiles'])),
                    'main_pool': [0], 'aux_pool': [1]})
    private = settings.snapshot()
    assert private['main_model']['api_key'] == 'secret-two'
    assert private['aux_model']['api_key'] == 'secret-one'


@pytest.mark.asyncio
async def test_real_runtime_keeps_working_after_browser_disconnect(tmp_path, monkeypatch, unused_tcp_port_factory):
    from backend.config import CONFIG
    from backend import runtime_settings
    from backend.server import KnoweServer
    # Fake project agents, real engine/queue/storage/socket gateway. No outside model calls.
    object.__setattr__(CONFIG, 'health_port', unused_tcp_port_factory())
    object.__setattr__(CONFIG, 'ws_port', unused_tcp_port_factory())
    object.__setattr__(CONFIG, 'script', 'full')
    data = tmp_path / 'data'
    object.__setattr__(CONFIG, 'data_dir', str(data))
    cfg = replace(config(tmp_path), runtime_token=CONFIG.runtime_token,
                  http_base=f'http://127.0.0.1:{CONFIG.health_port}', ws_url=f'ws://127.0.0.1:{CONFIG.ws_port}')
    monkeypatch.setenv('KNOWE_WEB_WORKSPACE_ROOT', str(cfg.workspace))
    runtime = KnoweServer()
    pid = 'project_20261112000001'
    async def until(ws, predicate):
        async with asyncio.timeout(10):
            async for message in ws:
                event = message.json()
                if predicate(event):
                    return event
        raise AssertionError('socket closed before expected event')
    async with TestClient(TestServer(create_app(cfg, runtime))) as client:
        origin = await login(client)
        # Set readiness after startup, and keep the platform model greeting out of this
        # project-engine test. FakeAgent does not use the binding's external endpoint.
        runtime_settings.apply({'main_model': {'provider': 'custom', 'model': 'test', 'api_key': 'test',
                                              'base_url': 'http://127.0.0.1:1', 'transport': 'openai_chat'}})
        ws = await client.ws_connect('/api/runtime/ws', origin=origin)
        await ws.send_json({'type': 'replay_request', 'project_id': '__platform__', 'since_seq': 0})
        await until(ws, lambda e: e.get('type') == 'replay_complete')
        await ws.send_json({'type': 'create_project', 'project_id': pid, 'project_name': '离线验证'})
        created = await until(ws, lambda e: e.get('type') == 'project_created' and e.get('project_id') == pid)
        assert Path(created['project_dir']).is_dir()
        await ws.send_json({'type': 'user_message', 'project_id': pid, 'content': '关闭页面后继续执行', 'client_msg_id': 'web-offline-1'})
        echo = await until(ws, lambda e: e.get('type') == 'user_echo')
        await ws.close()
        # Observe the authoritative engine, with no client connected. Do not assume a sleep
        # means success; wait for the exact reply and approval emitted by the engine.
        async with asyncio.timeout(10):
            while not any(event.get('type') == 'approval_card' for event in runtime.hub.projects[pid].ring.events()):
                await asyncio.sleep(.01)
        ws = await client.ws_connect('/api/runtime/ws', origin=origin)
        await ws.send_json({'type': 'replay_request', 'project_id': pid, 'since_seq': echo['seq']})
        events = []
        async with asyncio.timeout(10):
            async for message in ws:
                event = message.json()
                events.append(event)
                if event.get('type') == 'replay_complete':
                    break
        assert any(e.get('type') == 'message' and '关闭页面后继续执行' in e.get('content', '') for e in events)
        assert any(e.get('type') == 'approval_card' for e in events)
        await ws.close()
        notices = await (await client.get('/api/web/notifications')).json()
        assert notices['unread'] > 0 and notices['pending'] > 0


@pytest.mark.asyncio
async def test_password_change_revokes_sessions_without_changing_instance_identity(tmp_path):
    cfg = config(tmp_path)
    async with TestClient(TestServer(create_app(cfg))) as client:
        await login(client)
        before = await (await client.get('/api/web/session')).json()
        old_cookie = client.session.cookie_jar.filter_cookies(client.make_url('/'))[COOKIE].value
    async with TestClient(TestServer(create_app(replace(cfg, password='another-long-password')))) as client:
        after = await (await client.get('/api/web/session', headers={'Cookie': f'{COOKIE}={old_cookie}'})).json()
        assert after['authenticated'] is False
        assert after['instance_id'] == before['instance_id']


def test_recovered_approval_keeps_read_state_and_early_resolution_wins(tmp_path):
    state = WebState(tmp_path)
    card = {'type': 'approval_card', 'project_id': 'project_20261112000001',
            'card_id': 'stable-card', 'seq': 1, 'tool': 'dispatch', 'ts': '2026-11-12T00:00:00Z'}
    state.record(card)
    state.mark_read(through=state.list()['latest_id'])
    state.record({**card, 'seq': 10})
    assert len(state.list()['items']) == 1
    assert state.list()['unread'] == 0
    state.record({'type': 'approval_resolved', 'project_id': card['project_id'], 'card_id': 'early-card'})
    state = WebState(tmp_path)
    state.record({**card, 'card_id': 'early-card', 'seq': 20})
    early = next(item for item in state.list()['items'] if item['card_id'] == 'early-card')
    assert early['resolved'] == 1 and early['read'] == 1
    assert state.list()['pending'] == 0


@pytest.mark.asyncio
async def test_json_api_rejects_non_objects_and_chunked_oversize_body(tmp_path):
    async with TestClient(TestServer(create_app(config(tmp_path)))) as client:
        origin = str(client.make_url('/')).rstrip('/')
        for value in ([], None, 'password'):
            response = await client.post('/api/web/login', json=value, headers={'Origin': origin})
            assert response.status in {400, 415}
        async def oversized():
            yield b'{"password":"'
            yield b'x' * 5000
            yield b'"}'
        response = await client.post('/api/web/login', data=oversized(),
                                     headers={'Origin': origin, 'Content-Type': 'application/json'})
        assert response.status == 413
        await login(client)
        response = await client.post('/api/web/notifications/read', json=[], headers={'Origin': origin})
        assert response.status == 400
