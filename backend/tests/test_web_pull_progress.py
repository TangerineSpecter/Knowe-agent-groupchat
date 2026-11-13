"""Real Compose progress against a local Engine stub; no daemon, registry or credentials."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('API-Version', '1.48')
        self.send_header('OSType', 'linux')
        self.end_headers()

    def do_GET(self):
        if self.path.endswith('/version'):
            return self.respond({'ApiVersion': '1.48', 'Version': '28.0.0', 'Os': 'linux'})
        if '/images/' in self.path and self.path.endswith('/json'):
            if not self.server.pulled:
                return self.respond({'message': 'No such image'}, 404)
            return self.respond({'Id': 'sha256:' + 'a' * 64, 'Architecture': 'arm64', 'Os': 'linux',
                                 'Config': {'Labels': {}}, 'RootFS': {'Type': 'layers', 'Layers': []}})
        if '/images/json' in self.path or '/containers/json' in self.path:
            return self.respond([])
        return self.respond({'message': 'Unsupported test request'}, 404)

    def do_POST(self):
        if '/images/create' not in self.path:
            return self.respond({'message': 'Unsupported test request'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Connection', 'close')
        self.end_headers()
        for index in range(3 if self.server.stalled else 12):
            event = {'status': 'Downloading', 'id': 'slow-layer',
                     'progressDetail': {'current': (index + 1) * 1024 * 1024, 'total': 12 * 1024 * 1024}}
            self.wfile.write((json.dumps(event) + '\n').encode())
            self.wfile.flush()
            time.sleep(.3)
        if self.server.stalled:
            self.connection.settimeout(.2)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if self.connection.recv(1) == b'':
                        self.server.disconnected.set()
                        break
                except socket.timeout:
                    pass
            self.close_connection = True
            return
        self.server.pulled = True
        self.close_connection = True


@pytest.mark.parametrize('stalled', [False, True], ids=['slow-download', 'stalled-download'])
def test_native_compose_progress_controls_idle_deadline(tmp_path, stalled):
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker Compose CLI is optional for this integration test')
    config = tmp_path / 'docker-config'
    config.mkdir()
    # Reuse only plugin executables; the temporary config contains no registry auth.
    plugin_paths = [str(Path(docker).resolve().parent.parent / 'cli-plugins'),
                    str(Path.home() / '.docker/cli-plugins')]
    (config / 'config.json').write_text(json.dumps({'cliPluginsExtraDirs': plugin_paths}))
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
    environment['DOCKER_CONFIG'] = str(config)
    if subprocess.run([docker, 'compose', 'version'], env=environment, capture_output=True).returncode:
        pytest.skip('Docker Compose plugin is unavailable')
    source = (ROOT / 'deploy/install.sh').read_text()
    cleanup = source[source.index("deploy_pull_pid=''"):source.index('# Update only installer-owned keys.')]
    pull = source[source.index('pull_image() {'):source.index('\ndeploy_used_mirror=0')]
    script = tmp_path / 'pull.sh'
    script.write_text('set -eu\n' + cleanup + '\ndeploy_pull_timeout=2\n'
                      'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n' + pull
                      + '\npull_image mirror.example/slow-progress:test\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Engine)
    server.pulled = False
    server.stalled = stalled
    server.disconnected = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment['DOCKER_HOST'] = f'tcp://127.0.0.1:{server.server_port}'
    try:
        started = time.monotonic()
        result = subprocess.run(['sh', str(script)], env=environment,
                                text=True, capture_output=True, timeout=20)
        if stalled:
            assert result.returncode != 0, result.stdout + result.stderr
            assert time.monotonic() - started < 8
            assert server.disconnected.wait(1), 'Compose left the pull request running'
            assert '连续 2 秒没有新的镜像拉取进度' in result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert time.monotonic() - started > 3
            assert 'Downloading' in result.stdout and 'slow-layer' in result.stdout
            assert '停止本次拉取' not in result.stderr
    finally:
        server.shutdown()
        server.server_close()
