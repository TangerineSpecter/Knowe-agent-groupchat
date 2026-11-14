"""Single-user browser gateway. Runtime credentials never cross the HTTP boundary.

Run with PYTHONPATH=backend python -m backend.web; desktop still runs backend.server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

from .web_state import WebState
from .web_workspaces import contained_directory

log = logging.getLogger('knowe.web')
COOKIE = 'knowe_session'
SESSION_SECONDS = 7 * 24 * 3600
MAX_UPLOAD = 64 * 1024 * 1024


def diagnostic_origin(value: str | None) -> dict:
    """Log only a validated origin tuple, never raw untrusted header contents.

    This is diagnostic normalization only; authentication uses the original value.
    """
    if value is None:
        return {'state': 'missing'}
    if value == 'null':
        return {'state': 'null'}
    if not value:
        return {'state': 'empty'}
    if len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        return {'state': 'invalid'}
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {'http', 'https'} or not host
                or parsed.username is not None or parsed.password is not None):
            return {'state': 'invalid'}
        if ':' in host:
            host = str(ipaddress.IPv6Address(host)) if '%' not in host else ''
        elif not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
            return {'state': 'invalid'}
        if not host:
            return {'state': 'invalid'}
        port = parsed.port
        return {'state': 'valid', 'scheme': parsed.scheme, 'host': host,
                'port': port if port is not None else (443 if parsed.scheme == 'https' else 80)}
    except ValueError:
        return {'state': 'invalid'}


def log_origin_rejection(request: web.Request, config: WebConfig) -> None:
    # No path, query, body, credentials, cookies or arbitrary forwarded headers.
    details = {'origin': diagnostic_origin(request.headers.get('Origin')),
               'server': diagnostic_origin(f'{request.scheme}://{request.host}'),
               'configured': bool(config.public_origin),
               'expected': diagnostic_origin(config.public_origin or f'{request.scheme}://{request.host}')}
    log.warning('origin_rejected %s', json.dumps(details, ensure_ascii=True, sort_keys=True))


def persistent_secret(data: Path, name: str) -> str:
    target = data / 'web' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = target.read_text().strip()
        if not re_secret(value):
            raise RuntimeError(f'Invalid persisted web secret: {name}')
        return value
    value = secrets.token_hex(32)
    with os.fdopen(fd, 'w') as output:
        output.write(value)
    return value


def re_secret(value: str) -> bool:
    return len(value) == 64 and all(c in '0123456789abcdef' for c in value)


@dataclass(frozen=True)
class WebConfig:
    password: str
    secret: str
    workspace: Path
    data: Path
    dist: Path
    runtime_token: str
    http_base: str = 'http://127.0.0.1:8081'
    ws_url: str = 'ws://127.0.0.1:8080'
    public_origin: str = ''
    secure_cookie: bool = False

    def session(self) -> str:
        value = f'{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}'
        return value + '.' + hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticated(self, value: str) -> bool:
        try:
            expires, nonce, signature = value.split('.')
            expected = hmac.new(self.secret.encode(), f'{expires}.{nonce}'.encode(), hashlib.sha256).hexdigest()
            # Changing the configured password invalidates existing sessions too.
            return int(expires) > time.time() and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get('Origin', '')
        expected = self.public_origin or f'{request.scheme}://{request.host}'
        return origin == expected


def create_app(config: WebConfig, runtime=None, runtime_stopped: asyncio.Event | None = None) -> web.Application:
    config.workspace.mkdir(parents=True, exist_ok=True)
    state = WebState(config.data)
    from .file_library import FileLibrary
    library = FileLibrary(config.data)
    instance_id = hashlib.sha256(config.secret.encode()).hexdigest()[:16]
    # Include the password in the signing key, without storing it on disk.
    config = WebConfig(**{**config.__dict__, 'secret': hmac.new(
        config.secret.encode(), config.password.encode(), hashlib.sha256).hexdigest()})
    sockets: dict[str, set[web.WebSocketResponse]] = {}
    attempts: dict[str, tuple[int, float]] = {}
    client: ClientSession | None = None

    @web.middleware
    async def boundaries(request, handler):
        try:
            public = request.path in {'/api/web/session', '/api/web/login', '/healthz'}
            api = request.path.startswith('/api/')
            if api and not public:
                cookie = request.cookies.get(COOKIE, '')
                if not config.authenticated(cookie) or await asyncio.to_thread(state.revoked, hashlib.sha256(cookie.encode()).hexdigest()):
                    raise web.HTTPUnauthorized(text='登录已过期，请重新登录')
            if api and (request.method not in {'GET', 'HEAD'} or request.path == '/api/runtime/ws'):
                if not config.same_origin(request):
                    log_origin_rejection(request, config)
                    raise web.HTTPForbidden(text='请求来源不匹配，请从部署地址重新打开页面')
            response = await handler(request)
        except (ValueError, OSError) as exc:
            response = web.json_response({'error': str(exc)}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text}, status=exc.status)
        # Generated HTML is always sandboxed in the preview UI; never serve it as app HTML.
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.path.startswith('/api/runtime/preview'):
            response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    app = web.Application(middlewares=[boundaries], client_max_size=MAX_UPLOAD + 1024 * 1024)

    async def object_body(request: web.Request) -> dict:
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType(text='请求需要使用 JSON 格式')
        # Bound actual bytes too: chunked requests need not have Content-Length.
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('请求内容必须是 JSON 对象')
        return payload

    async def session(request):
        cookie = request.cookies.get(COOKIE, '')
        return web.json_response({'web': True, 'authenticated': config.authenticated(cookie)
                                  and not await asyncio.to_thread(state.revoked, hashlib.sha256(cookie.encode()).hexdigest()),
                                  'instance_id': instance_id})

    async def login(request):
        if request.content_length and request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length)
        peer = request.remote or 'unknown'
        count, start = attempts.get(peer, (0, time.monotonic()))
        if time.monotonic() - start > 60:
            count, start = 0, time.monotonic()
        if count >= 10:
            raise web.HTTPTooManyRequests(text='尝试次数过多，请一分钟后重试')
        payload = await object_body(request)
        supplied = str(payload.get('password', ''))
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),
                                   hashlib.sha256(config.password.encode()).digest()):
            if len(attempts) > 1024:
                attempts.clear()
            attempts[peer] = (count + 1, start)
            raise web.HTTPUnauthorized(text='访问密码不正确')
        attempts.pop(peer, None)
        response = web.json_response({'ok': True})
        response.set_cookie(COOKIE, config.session(), max_age=SESSION_SECONDS,
                            httponly=True, secure=config.secure_cookie, samesite='Strict', path='/')
        return response

    async def logout(request):
        cookie = request.cookies.get(COOKIE, '')
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        await asyncio.to_thread(state.revoke, digest, time.time() + SESSION_SECONDS)
        for socket in list(sockets.get(digest, ())):
            await socket.close(code=4003, message=b'logged out')
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    async def workspaces(request):
        path = contained_directory(config.workspace, request.query.get('path', ''))
        children = await asyncio.to_thread(lambda: sorted(
            child.name for child in path.iterdir()
            if not child.name.startswith('.') and child.is_dir()
            and child.resolve().is_relative_to(config.workspace)))
        return web.json_response({'path': str(path), 'root': str(config.workspace), 'directories': children})

    async def notifications(request):
        result = await asyncio.to_thread(state.list, int(request.query.get('before', '0')))
        return web.json_response(result)

    async def read_notifications(request):
        payload = await object_body(request)
        await asyncio.to_thread(state.mark_read, item_id=int(payload.get('id', 0)), through=int(payload.get('through', 0)))
        return web.json_response({'ok': True})

    async def upload(request):
        from .attachments import sign_path
        from .config import CONFIG
        # Never accept a destination path from a client. A random per-upload directory
        # avoids overwriting a project file or following an attacker-chosen symlink.
        directory = config.workspace / 'uploads' / secrets.token_hex(16)
        (config.workspace / 'uploads').mkdir(exist_ok=True)
        contained_directory(config.workspace, str(directory.parent))
        directory.mkdir()
        files = []
        total = 0
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.name != 'files' or not part.filename:
                    raise ValueError('请选择要上传的文件')
                name = Path(part.filename.replace('\\', '/')).name
                if name in {'', '.', '..'} or '\x00' in name or len(name.encode()) > 240:
                    raise ValueError('文件名无效或过长')
                target = directory / name
                size = 0
                with target.open('xb') as output:
                    while chunk := await part.read_chunk(256 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > MAX_UPLOAD:
                            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=total)
                        await asyncio.to_thread(output.write, chunk)
                files.append({'path': str(target), 'name': name, 'size': size,
                              'ext': target.suffix.lstrip('.'),
                              'sig': sign_path(str(target), CONFIG.attachment_key or config.runtime_token)})
                if len(files) > 20:
                    raise ValueError('每次最多上传 20 个文件')
            if not files:
                raise ValueError('没有收到文件')
            for item in files:
                await asyncio.to_thread(library.register, Path(item['path']), origin='upload',
                                        managed_root=config.workspace / 'uploads')
            return web.json_response({'files': files})
        except BaseException:
            import shutil
            await asyncio.to_thread(shutil.rmtree, directory, True)
            raise

    async def discard_upload(request):
        from .attachments import verify_path
        from .config import CONFIG
        payload = await object_body(request)
        raw, sig = str(payload.get('path', '')), str(payload.get('sig', ''))
        if not verify_path(raw, sig, CONFIG.attachment_key or config.runtime_token):
            raise web.HTTPForbidden(text='附件凭据无效')
        # Synchronize persisted message references before considering a file abandoned.
        if runtime is not None:
            from .library_api import sync
            await asyncio.to_thread(sync, runtime)
        removed = await asyncio.to_thread(library.discard_upload, Path(raw), config.workspace / 'uploads')
        return web.json_response({'ok': True, 'removed': removed})

    async def attachment(request):
        from .attachments import verify_path
        from .config import CONFIG
        raw = request.query.get('path', '')
        sig = request.query.get('sig', '')
        if not verify_path(raw, sig, CONFIG.attachment_key or config.runtime_token):
            raise web.HTTPForbidden(text='附件凭据无效')
        target = Path(raw).resolve()
        if not target.is_relative_to(config.workspace / 'uploads') or not target.is_file():
            raise web.HTTPNotFound(text='附件已不存在')
        response = web.FileResponse(target)
        response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(target.name, safe='')
        return response

    async def library_download(request):
        target, _ = await asyncio.to_thread(library.resolve, request.query.get('id', ''))
        response = web.FileResponse(target)
        response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(target.name, safe='')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    async def proxy(request):
        assert client is not None
        suffix = request.match_info['path']
        # No desktop process-control or OS file-manager operations through the browser.
        if suffix in {'shutdown', 'files/reveal'} or suffix.startswith(('api/web', 'api/runtime')):
            raise web.HTTPNotFound()
        upstream = config.http_base + '/' + suffix
        params = [(key, value) for key, value in request.query.items() if key not in {'token', '_kpt'}]
        headers = {'X-Knowe-Runtime-Token': config.runtime_token}
        if 'Content-Type' in request.headers:
            headers['Content-Type'] = request.headers['Content-Type']
        try:
            async with client.request(request.method, upstream, params=params, headers=headers,
                                      data=await request.read(), allow_redirects=False) as result:
                forwarded = {key: result.headers[key] for key in ('Content-Type', 'Content-Disposition') if key in result.headers}
                # Buffer bounded runtime responses. The runtime preview already caps files at 64 MiB.
                body = await result.read()
                if suffix == 'preview' and request.query.get('download') == '1':
                    disposition = forwarded.get('Content-Disposition', '')
                    if disposition.startswith('inline'):
                        forwarded['Content-Disposition'] = 'attachment' + disposition[len('inline'):]
                    elif not disposition:
                        name = Path(request.query.get('path', 'file')).name
                        forwarded['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(name, safe='')
                return web.Response(body=body, status=result.status, headers=forwarded)
        except (ClientError, OSError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text='后端暂时不可用，请稍后重试')

    async def websocket(request):
        assert client is not None
        try:
            upstream = await client.ws_connect(config.ws_url, headers={'X-Knowe-Runtime-Token': config.runtime_token},
                                               max_msg_size=16 * 1024 * 1024)
        except (ClientError, OSError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text='任务服务暂时不可用')
        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await downstream.prepare(request)
        cookie = request.cookies.get(COOKIE, '')
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        sockets.setdefault(digest, set()).add(downstream)

        async def relay(source, target, from_browser=False):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    if from_browser:
                        try:
                            payload = json.loads(message.data)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict) or payload.get('type') == 'shutdown':
                            continue
                    await target.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    # Browser commands use JSON text only; binary JSON must not bypass
                    # the command filter above (notably the desktop shutdown command).
                    if not from_browser:
                        await target.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break
        async def expire():
            while config.authenticated(cookie):
                await asyncio.sleep(30)
            await downstream.close(code=4003, message=b'session expired')

        tasks = [asyncio.create_task(expire()), asyncio.create_task(relay(downstream, upstream, True)),
                 asyncio.create_task(relay(upstream, downstream))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
            sockets.get(digest, set()).discard(downstream)
            if not sockets.get(digest):
                sockets.pop(digest, None)
        return downstream

    async def static(request):
        if request.path.startswith('/api/'):
            raise web.HTTPNotFound()
        raw = request.match_info.get('path', '') or 'index.html'
        target = (config.dist / raw).resolve()
        if not target.is_relative_to(config.dist.resolve()) or raw.startswith('.'):
            raise web.HTTPNotFound()
        if not target.is_file():
            if '.' in Path(raw).name:
                raise web.HTTPNotFound()
            target = config.dist / 'index.html'
        if not target.is_file():
            raise web.HTTPServiceUnavailable(text='前端尚未构建，请先执行 npm run web:build')
        response = web.FileResponse(target)
        response.headers['Cache-Control'] = 'no-cache' if target.suffix == '.html' else 'public, max-age=3600'
        return response

    async def health(request):
        if runtime is not None and (runtime_task is None or runtime_task.done()):
            raise web.HTTPServiceUnavailable(text='runtime unavailable')
        return web.json_response({'ok': True})

    runtime_task = None

    async def lifespan(app):
        nonlocal client, runtime_task
        client = ClientSession(timeout=ClientTimeout(total=300, connect=10), trust_env=False)
        try:
            if runtime is not None:
                async def observe(event):
                    if event.get('type') in {'approval_card', 'approval_resolved', 'error', 'completion_status'}:
                        await asyncio.to_thread(state.record, event)
                runtime.hub.event_listeners.append(observe)
                runtime_task = asyncio.create_task(runtime.run(install_signals=False), name='knowe-runtime')
                if runtime_stopped is not None:
                    runtime_task.add_done_callback(lambda _: runtime_stopped.set())
                # Do not expose a healthy login page while the engine failed to boot.
                for _ in range(600):
                    if runtime_task.done():
                        await runtime_task
                        raise RuntimeError('Runtime exited during startup')
                    try:
                        async with client.get(config.http_base + '/health', headers={'X-Knowe-Runtime-Token': config.runtime_token},
                                              timeout=ClientTimeout(total=1)) as result:
                            if result.status == 200:
                                break
                    except (ClientError, OSError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError('Runtime startup timed out')
                # Replay into an idempotent projection: notifications survive pages being closed,
                # and a restart can repair the gap between event persistence and projection.
                for pid in list(runtime.hub.projects):
                    for event in await asyncio.to_thread(runtime.hub.durable_since, pid, 0):
                        await asyncio.to_thread(state.record, event)
            yield
        finally:
            if runtime is not None and runtime_task is not None:
                runtime._shutdown_event.set()
                try:
                    await asyncio.wait_for(asyncio.shield(runtime_task), timeout=25)
                except asyncio.TimeoutError:
                    runtime_task.cancel()
                    await asyncio.gather(runtime_task, return_exceptions=True)
                except Exception:
                    log.exception('Runtime exited unexpectedly')
            await client.close()

    app.cleanup_ctx.append(lifespan)
    app.router.add_get('/healthz', health)
    app.router.add_get('/api/web/session', session)
    app.router.add_post('/api/web/login', login)
    app.router.add_post('/api/web/logout', logout)
    app.router.add_get('/api/web/workspaces', workspaces)
    app.router.add_get('/api/web/notifications', notifications)
    app.router.add_post('/api/web/notifications/read', read_notifications)
    app.router.add_post('/api/web/uploads', upload)
    app.router.add_post('/api/web/uploads/discard', discard_upload)
    app.router.add_get('/api/web/attachments', attachment)
    app.router.add_get('/api/web/library/download', library_download)
    app.router.add_get('/api/runtime/ws', websocket)
    app.router.add_route('*', '/api/runtime/{path:.*}', proxy)
    app.router.add_get('/{path:.*}', static)
    return app


async def serve(config: WebConfig, runtime, *, host: str, port: int) -> None:
    """A dead runtime must terminate the container, not leave a healthy-looking login shell."""
    stopped = asyncio.Event()
    interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, interrupted.set)
    runner = web.AppRunner(create_app(config, runtime, stopped), access_log_format='%a %s %b')
    waits = []
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info('Web ready at http://%s:%d', host, port)
        waits = [asyncio.create_task(stopped.wait()), asyncio.create_task(interrupted.wait())]
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set() and not interrupted.is_set():
            raise RuntimeError('Runtime stopped unexpectedly; shutting down web gateway')
    finally:
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
        await runner.cleanup()


def main():
    # The desktop entry is intentionally untouched. Configure the runtime before importing it.
    data = Path(os.environ.get('KNOWE_DATA_DIR', './data')).resolve()
    workspace = Path(os.environ.get('KNOWE_WEB_WORKSPACE_ROOT', './workspaces')).resolve()
    password_file = os.environ.get('KNOWE_WEB_PASSWORD_FILE', '')
    password = Path(password_file).read_text().strip() if password_file else os.environ.get('KNOWE_WEB_PASSWORD', '')
    if len(password) < 12:
        raise RuntimeError('Set KNOWE_WEB_PASSWORD (at least 12 characters) or KNOWE_WEB_PASSWORD_FILE')
    token = secrets.token_hex(32)
    os.environ.update(KNOWE_DATA_DIR=str(data), KNOWE_WEB_WORKSPACE_ROOT=str(workspace),
                      KNOWE_RUNTIME_TOKEN=token, KNOWE_ATTACHMENT_KEY=persistent_secret(data, 'attachment.key'),
                      KNOWE_WS_HOST='127.0.0.1', KNOWE_HEALTH_HOST='127.0.0.1')
    from .config import CONFIG
    from .server import KnoweServer
    config = WebConfig(password=password, secret=persistent_secret(data, 'session.key'),
                       workspace=workspace, data=data, runtime_token=token,
                       dist=Path(os.environ.get('KNOWE_WEB_DIST', 'dist')).resolve(),
                       http_base=f'http://127.0.0.1:{CONFIG.health_port}', ws_url=f'ws://127.0.0.1:{CONFIG.ws_port}',
                       public_origin=os.environ.get('KNOWE_WEB_ORIGIN', '').rstrip('/'),
                       secure_cookie=os.environ.get('KNOWE_WEB_SECURE_COOKIE', '0') == '1')
    logging.basicConfig(level=logging.INFO)
    # Access logs omit query strings (which may contain project filenames).
    asyncio.run(serve(config, KnoweServer(), host=os.environ.get('KNOWE_WEB_HOST', '127.0.0.1'),
                      port=int(os.environ.get('KNOWE_WEB_PORT', '8090'))))


if __name__ == '__main__':
    main()
