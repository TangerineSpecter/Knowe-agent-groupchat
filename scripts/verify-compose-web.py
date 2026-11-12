#!/usr/bin/env python3
"""Isolated Compose acceptance test. Never mounts an existing Knowe data directory.

Requires a built web image and the Python web dependencies. Uses a mock provider
inside its own Compose network; no user model credentials or external model calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import textwrap
from urllib.parse import urlencode

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PID = 'project_20261112000002'

MOCK_PROVIDER = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        text='Local Compose acceptance model reply'
        if body.get('stream'):
            chunks=[{'choices':[{'index':0,'delta':{'content':text},'finish_reason':None}]},
                    {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
            payload=(''.join('data: '+json.dumps(c)+'\\n\\n' for c in chunks)+'data: [DONE]\\n\\n').encode()
            content_type='text/event-stream'
        else:
            payload=json.dumps({'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}).encode()
            content_type='application/json'
        self.send_response(200);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8082),Handler).serve_forever()
'''


async def receive_until(ws, predicate):
    async with asyncio.timeout(20):
        async for message in ws:
            event = message.json()
            if predicate(event):
                return event
    raise AssertionError('WebSocket closed before expected event')


async def check_first_run(origin: str, password: str):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        headers = {'Origin': origin}
        response = await client.post(origin + '/api/web/login', json={'password': password}, headers=headers)
        assert response.status == 200, 'login failed'
        binding = {'provider': 'custom', 'model': 'compose-test', 'api_key': 'compose-test-key',
                   'base_url': 'http://model-mock:8082', 'transport': 'openai_chat'}
        response = await client.post(origin + '/api/runtime/settings/test',
                                     json={'target': 'main', 'binding': binding}, headers=headers)
        assert (await response.json())['ok'], 'model probe failed'
        response = await client.post(origin + '/api/runtime/settings',
                                     json={'model_profiles': [binding], 'main_pool': [0], 'aux_pool': [],
                                           'approval_timeout_s': 0}, headers=headers)
        result = await response.json()
        assert result['ok'] and result['zinnia_compatible'], 'model apply failed'
        async with client.ws_connect(origin + '/api/runtime/ws', origin=origin) as ws:
            await ws.send_json({'type': 'replay_request', 'project_id': '__platform__', 'since_seq': 0})
            await receive_until(ws, lambda e: e.get('type') == 'replay_complete')
            await ws.send_json({'type': 'create_project', 'project_id': PID, 'project_name': 'Compose acceptance'})
            event = await receive_until(ws, lambda e: e.get('type') == 'project_created' and e.get('project_id') == PID)
            assert event['project_dir'] == f'/workspace/projects/{PID}'
            await ws.send_json({'type': 'user_message', 'project_id': PID, 'client_msg_id': 'compose-offline-1',
                                'content': 'Compose offline execution check'})
            echo = await receive_until(ws, lambda e: e.get('type') == 'user_echo')
        # There are no WebSocket clients while the engine creates its approval.
        async with asyncio.timeout(20):
            while True:
                notices = await (await client.get(origin + '/api/web/notifications')).json()
                if notices['pending']:
                    break
                await asyncio.sleep(.05)
        async with client.ws_connect(origin + '/api/runtime/ws', origin=origin) as ws:
            await ws.send_json({'type': 'replay_request', 'project_id': PID, 'since_seq': echo['seq']})
            reply = await receive_until(ws, lambda e: e.get('type') == 'message' and 'Compose offline execution check' in e.get('content', ''))
        response = await client.post(origin + '/api/web/notifications/read', headers=headers,
                                     json={'through': notices['latest_id']})
        assert response.status == 200
        form = aiohttp.FormData()
        form.add_field('files', b'Compose persistent upload', filename='compose-upload.txt', content_type='text/plain')
        response = await client.post(origin + '/api/web/uploads', data=form, headers=headers)
        assert response.status == 200, 'upload failed'
        file = (await response.json())['files'][0]
        cookie = next(c.value for c in client.cookie_jar if c.key == 'knowe_session')
        return {'cookie': cookie, 'file': file, 'reply': reply['content'],
                'notification_ids': {row['id'] for row in notices['items']}}


async def check_after_recreation(origin: str, state: dict):
    async with aiohttp.ClientSession(headers={'Cookie': 'knowe_session=' + state['cookie']}) as client:
        session = await (await client.get(origin + '/api/web/session')).json()
        assert session['authenticated'], 'session did not survive recreation'
        settings = await (await client.get(origin + '/api/runtime/settings')).json()
        assert settings['main_model']['has_api_key'] and 'compose-test-key' not in str(settings)
        file = state['file']
        response = await client.get(origin + '/api/web/attachments', params={'path': file['path'], 'sig': file['sig']})
        assert response.status == 200 and await response.read() == b'Compose persistent upload'
        notices = await (await client.get(origin + '/api/web/notifications')).json()
        assert state['notification_ids'] <= {row['id'] for row in notices['items']}
        assert all(row['read'] for row in notices['items'] if row['id'] in state['notification_ids'])
        async with client.ws_connect(origin + '/api/runtime/ws', origin=origin) as ws:
            await ws.send_json({'type': 'replay_request', 'project_id': PID, 'since_seq': 0})
            await receive_until(ws, lambda e: e.get('type') == 'message' and e.get('content') == state['reply'])


def check_browser(origin: str, password: str, directory: Path, artifacts: Path, executable: str | None):
    from playwright.sync_api import expect, sync_playwright
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        options = {'headless': True}
        if executable:
            options['executable_path'] = executable
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(origin)
            page.wait_for_load_state('networkidle')
            page.get_by_label('访问密码', exact=True).fill(password)
            page.get_by_role('button', name='进入工作空间', exact=True).click()
            expect(page.get_by_role('button', name='新建项目', exact=True)).to_be_visible()
            expect(page.locator('.frg-card')).not_to_be_visible()
            page.get_by_role('button', name='新建项目', exact=True).click()
            modal = page.locator('.modal-np')
            modal.locator('#np-name').fill('浏览器已有目录验收')
            modal.get_by_label('项目文件夹创建方式').select_option('existing')
            modal.get_by_role('button', name='projects/', exact=True).click()
            modal.get_by_role('button', name=PID + '/', exact=True).click()
            modal.get_by_role('button', name='使用当前文件夹', exact=True).click()
            modal.get_by_role('button', name='创建', exact=True).click()
            expect(modal).not_to_be_visible()
            expect(page.get_by_role('button', name='项目 浏览器已有目录验收', exact=True)).to_be_visible()
            page.get_by_label('上传附件', exact=True).set_input_files({
                'name': 'browser-upload.txt', 'mimeType': 'text/plain', 'buffer': b'Browser upload works'})
            expect(page.locator('.attach-chip').get_by_text('browser-upload.txt', exact=True)).to_be_visible()
            page.get_by_role('button', name='通知中心', exact=False).click()
            expect(page.locator('#web-notifications article').first).to_be_visible()
            page.screenshot(path=str(artifacts / 'compose-web-notifications.png'), full_page=True)
            fixture = directory / 'workspaces' / 'projects' / PID / 'browser-preview.txt'
            fixture.write_text('Container web preview and download', encoding='utf-8')
            preview = context.new_page()
            payload = {'projectId': PID, 'sourceKey': 'browser-fixture',
                       'file': {'path': fixture.name, 'name': fixture.name}}
            preview.goto(origin + '/#' + urlencode({'preview': json.dumps(payload)}))
            expect(preview.get_by_text('Container web preview and download', exact=True)).to_be_visible()
            with preview.expect_download() as download:
                preview.get_by_role('button', name='下载文件', exact=True).click()
            assert Path(download.value.path()).read_text() == 'Container web preview and download'
            preview.close()
            page.locator('#web-notifications').get_by_role('button', name='关闭', exact=True).click()
            page.locator('.rail button[data-view="settings"]').click()
            page.locator('.plm:visible input[value="en"]').check()
            with page.expect_response(lambda response: response.url.endswith('/api/runtime/settings')
                                      and response.request.method == 'POST'):
                page.locator('.plm:visible .plm-apply').click()
            page.locator('.rail button[aria-controls="web-notifications"]').click()
            expect(page.locator('#web-notifications').get_by_role('heading', name='Notifications')).to_be_visible()
            page.set_viewport_size({'width': 1024, 'height': 768})
            page.evaluate('''async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); }''')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'page overflows horizontally'
            assert page.locator('#web-notifications').bounding_box()['height'] == 768
            page.screenshot(path=str(artifacts / 'compose-web-english.png'), full_page=True)
            # A language/preferences save must not turn "never time out" back into 300 s.
            settings = context.request.get(origin + '/api/runtime/settings').json()
            assert settings['approval_timeout_s'] is None
            other = context.new_page()
            other.goto(origin)
            other.wait_for_load_state('networkidle')
            page.locator('#web-notifications').get_by_role('button', name='Sign out').click()
            expect(page.get_by_label('Access password', exact=True)).to_be_visible(timeout=10000)
            expect(other.get_by_label('Access password', exact=True)).to_be_visible(timeout=10000)
            assert not errors, errors
            print('PASS: browser login/bootstrap, existing directory, upload, notifications, preview/download, English and cross-tab logout', flush=True)
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='knowe-web:validation', help='Already built web image (no image build is performed)')
    parser.add_argument('--port', type=int, default=0, help='Loopback HTTP port; 0 chooses a free port')
    parser.add_argument('--browser', action='store_true', help='Also verify the UI with Playwright Chromium installed on the host')
    parser.add_argument('--browser-path', help='Also verify the real browser UI using this Chromium/Chrome executable')
    parser.add_argument('--artifacts-dir', default=str(Path(tempfile.gettempdir()) / 'knowe-compose-browser'),
                        help='Browser screenshot output directory')
    args = parser.parse_args()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
        port = probe.getsockname()[1]
    project = 'knowe-qa-' + secrets.token_hex(4)
    with tempfile.TemporaryDirectory(prefix='knowe-compose-qa-') as name:
        directory = Path(name)
        uid, gid = (os.getuid(), os.getgid()) if os.getuid() else (1000, 1000)
        for entry in ('data', 'workspaces'):
            (directory / entry).mkdir()
            if os.getuid() == 0:
                os.chown(directory / entry, uid, gid)
        password = secrets.token_hex(24)
        secret = directory / 'password'
        secret.write_text(password)
        secret.chmod(0o600)
        if os.getuid() == 0:
            os.chown(secret, uid, gid)
        provider = directory / 'model-mock.py'
        provider.write_text(MOCK_PROVIDER)
        provider.chmod(0o644)
        environment = directory / 'qa.env'
        environment.write_text(f'''KNOWE_UID={uid}
KNOWE_GID={gid}
KNOWE_PORT={port}
KNOWE_BIND_HOST=127.0.0.1
KNOWE_DATA_PATH={directory}/data
KNOWE_WORKSPACE_PATH={directory}/workspaces
KNOWE_PASSWORD_PATH={secret}
''')
        override = directory / 'override.json'
        override.write_text(json.dumps({'services': {
            'knowe': {'image': args.image, 'environment': {'KNOWE_AGENT': 'fake', 'KNOWE_SCRIPT': 'full',
                      'KNOWE_KICKOFF': '0', 'KNOWE_FAKE_DELAY': '0.01'},
                      'depends_on': {'model-mock': {'condition': 'service_healthy'}}},
            'model-mock': {'image': args.image, 'command': ['python', '/qa/model-mock.py'],
                          'volumes': [f'{provider}:/qa/model-mock.py:ro'],
                          'healthcheck': {'test': ['CMD', 'python', '-c',
                              "import socket; socket.create_connection(('127.0.0.1',8082),timeout=2).close()"],
                              'interval': '2s', 'timeout': '3s', 'retries': 10, 'start_period': '3s'}},
        }}))
        command = ['docker', 'compose', '--env-file', str(environment), '-f', str(ROOT / 'compose.yaml'),
                   '-f', str(override), '-p', project]
        def run(*arguments, source=None):
            subprocess.run([*command, *arguments], input=source, text=True, check=True, cwd=ROOT)
        try:
            run('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
            origin = f'http://127.0.0.1:{port}'
            state = asyncio.run(check_first_run(origin, password))
            assert (directory / 'workspaces' / 'projects' / PID).is_dir()
            print('PASS: login, model probe/apply, mapped project directory, offline task/replay, notifications, upload', flush=True)
            run('exec', '-T', 'knowe', 'python', '-', source=textwrap.dedent('''\
                import os, subprocess
                from pathlib import Path
                from playwright.sync_api import sync_playwright
                assert os.getuid() != 0
                Path('/workspace/qa-python.txt').write_text('Python works')
                subprocess.run(['node', '-e', "require('fs').writeFileSync('/workspace/qa-node.txt','Node works')"], check=True)
                subprocess.run(['npm', '--version'], check=True)
                subprocess.run(['git', '--version'], check=True)
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.set_content('<h1>Container browser works</h1>')
                    assert page.locator('h1').inner_text() == 'Container browser works'
                    browser.close()
                print('PASS: non-root Python / Node / npm / Git / Chromium')
                '''))
            run('up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '180', 'knowe')
            asyncio.run(check_after_recreation(origin, state))
            print('PASS: recreation retains login, credentials, signed uploads, reply and notification read state', flush=True)
            if args.browser or args.browser_path:
                check_browser(origin, password, directory, Path(args.artifacts_dir), args.browser_path)
        finally:
            artifacts = Path(args.artifacts_dir)
            artifacts.mkdir(parents=True, exist_ok=True)
            logs = subprocess.run([*command, 'logs', '--no-color'], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
            (artifacts / 'compose.log').write_text(logs.stdout, encoding='utf-8')
            run('down', '--volumes', '--remove-orphans', '--timeout', '40')


if __name__ == '__main__':
    main()
