"""Run the installer in isolated checkouts; mock Docker, exercise actual shell flow."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
MOCK_DOCKER = r'''
import json, os, pathlib, sys, time
args=sys.argv[1:]
root=pathlib.Path.cwd()
with open(os.environ['QA_LOG'], 'a') as output:
    output.write(json.dumps({'args':args, 'port':os.environ.get('KNOWE_PORT'),
        'node':os.environ.get('KNOWE_NODE_IMAGE'), 'python':os.environ.get('KNOWE_PYTHON_IMAGE')})+'\n')
if args == ['info']:
    sys.exit(int(os.environ.get('QA_DAEMON_FAIL','0')))
if args[:2] == ['compose','version']:
    sys.exit(0)
if args[:3] == ['compose','config','--environment']:
    values={}
    for line in (root/'.env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.removeprefix('export ').split('=',1)
            values[key]=value.strip().strip('"').strip("'")
    values.update({k:v for k,v in os.environ.items() if k.startswith('KNOWE_')})
    for key,value in values.items(): print(key+'='+value)
    sys.exit(0)
if args[:2] == ['compose','ps']:
    print(os.environ.get('QA_OWN_CONTAINER',''))
    sys.exit(0)
if args[:2] == ['compose','port']:
    print(os.environ.get('QA_OWN_BINDING',''))
    sys.exit(0)
if args[:2] == ['ps','--format']:
    for port in os.environ.get('QA_DOCKER_PORTS','').split(','):
        if port: print('0.0.0.0:'+port+'->8090/tcp')
    sys.exit(0)
if args[:2] == ['image','inspect']:
    sys.exit(1 if os.environ.get('QA_UNCACHED') else 0)
if args[:1] == ['pull']:
    image=args[1]
    is_mirror=image.startswith('m.daocloud.io/')
    mode=os.environ.get('QA_MIRROR_PULL' if is_mirror else 'QA_OFFICIAL_PULL','ok')
    if mode == 'hang':
        with open(os.environ['QA_PULL_PIDS'],'a') as output: output.write(str(os.getpid())+'\n')
        time.sleep(30)
    sys.exit(1 if mode == 'fail' else 0)
if args[:4] == ['compose','--progress','plain','build']:
    if os.environ.get('QA_BUILD_RACE'): (root/'build-race').touch()
    sys.exit(int(os.environ.get('QA_BUILD_FAIL','0')))
if args[:2] == ['compose','up']:
    sys.exit(0)
raise SystemExit('Unexpected docker invocation: '+str(args))
'''
MOCK_SS = r'''
import os, pathlib
ports=os.environ.get('QA_NATIVE_PORTS','').split(',')
if pathlib.Path('build-race').exists(): ports += [os.environ.get('QA_BUILD_RACE','')]
for port in ports:
    if port: print('LISTEN 0 128 [::]:'+port+' [::]:*')
'''


@pytest.fixture
def installer(tmp_path):
    directory = tmp_path / 'checkout with spaces'
    (directory / 'deploy').mkdir(parents=True)
    shutil.copy2(ROOT / 'deploy/install.sh', directory / 'deploy/install.sh')
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name, source in {'docker': MOCK_DOCKER, 'ss': MOCK_SS}.items():
        target = binary / name
        target.write_text(f'#!{sys.executable}\n' + source)
        target.chmod(0o755)
    for name, source in {
        'id': '#!/bin/sh\nif [ "$1" = -u ]; then echo "${QA_UID:-1000}"; else echo "${QA_GID:-1000}"; fi\n',
        'chown': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$QA_CHOWN"\n',
        'openssl': '#!/bin/sh\nprintf "%s\\n" qa-install-password-1234567890\n',
    }.items():
        target = binary / name
        target.write_text(source)
        target.chmod(0o755)
    environment = {k:v for k,v in os.environ.items() if not k.startswith(('KNOWE_', 'COMPOSE_'))}
    environment.update(PATH=str(binary) + ':' + environment['PATH'],
                       QA_LOG=str(tmp_path / 'docker.jsonl'), QA_CHOWN=str(tmp_path / 'chown.log'),
                       QA_PULL_PIDS=str(tmp_path / 'pull-pids'))

    class Installer:
        root = directory
        bin = binary
        env = environment

        def run(self, **changes):
            return subprocess.run(['sh', str(self.root / 'deploy/install.sh')], cwd=tmp_path,
                                  env={**self.env, **changes}, text=True, capture_output=True, timeout=25)

        def config(self, content):
            (self.root / '.env').write_text(content)

        def values(self):
            return dict(line.split('=', 1) for line in (self.root / '.env').read_text().splitlines()
                        if '=' in line and not line.startswith('#'))

        def calls(self):
            return [json.loads(line) for line in Path(self.env['QA_LOG']).read_text().splitlines()]

    return Installer()


def succeeded(result):
    assert result.returncode == 0, result.stdout + result.stderr


def test_fresh_install_root_uses_nonroot_and_actual_new_port(installer):
    result = installer.run(QA_UID='0', QA_GID='0')
    succeeded(result)
    assert installer.values()['KNOWE_PORT'] == '18090'
    assert installer.values()['KNOWE_UID'] == '1000'
    assert 'http://127.0.0.1:18090' in result.stdout
    assert len(Path(installer.env['QA_CHOWN']).read_text().splitlines()) == 3
    assert (installer.root / 'deploy/web-password').stat().st_mode & 0o777 == 0o600
    calls = installer.calls()
    build = next(i for i,c in enumerate(calls) if 'build' in c['args'])
    up = next(i for i,c in enumerate(calls) if 'up' in c['args'])
    assert build < up and calls[up]['port'] == '18090'


def test_legacy_failed_install_migrates_and_repeated_run_preserves_data(installer):
    installer.config('KNOWE_PORT=8090\nUSER_VALUE=$(touch should-not-execute)\n')
    password = installer.root / 'deploy/web-password'
    password.write_text('existing-private-password')
    data = installer.root / 'deploy/data'
    data.mkdir()
    (data / 'keep.txt').write_text('persistent data')
    first = installer.run()
    succeeded(first)
    assert installer.values()['KNOWE_PORT'] == '18090'
    before = (installer.root / '.env').read_bytes()
    succeeded(installer.run())
    assert (installer.root / '.env').read_bytes() == before
    assert password.read_text() == 'existing-private-password'
    assert (data / 'keep.txt').read_text() == 'persistent data'
    assert not (installer.root / 'should-not-execute').exists()


@pytest.mark.parametrize('has_container', [True, False])
def test_existing_deployment_retains_8090_and_own_listener(installer, has_container):
    installer.config('KNOWE_PORT=8090\n')
    state = installer.root / 'deploy/data/web'
    state.mkdir(parents=True)
    (state / 'session.key').write_text('persisted web instance')
    changes = {'QA_OWN_CONTAINER': 'existing-knowe', 'QA_OWN_BINDING': '127.0.0.1:8090',
               'QA_DOCKER_PORTS': '8090', 'QA_NATIVE_PORTS': '8090'} if has_container else {}
    succeeded(installer.run(**changes))
    assert installer.values()['KNOWE_PORT'] == '8090'


def test_avoids_docker_native_and_ipv6_listeners(installer):
    result = installer.run(QA_DOCKER_PORTS='18090', QA_NATIVE_PORTS='18091,18092')
    succeeded(result)
    assert installer.values()['KNOWE_PORT'] == '18093'
    assert 'http://127.0.0.1:18093' in result.stdout
    assert next(c for c in installer.calls() if 'up' in c['args'])['port'] == '18093'


def test_avoids_a_published_docker_port_range(installer):
    succeeded(installer.run(QA_DOCKER_PORTS='18090-18092'))
    assert installer.values()['KNOWE_PORT'] == '18093'


def test_rechecks_port_after_long_build_and_overrides_exported_old_value(installer):
    result = installer.run(KNOWE_PORT='19000', QA_BUILD_RACE='19000', KNOWE_BIND_HOST='0.0.0.0')
    succeeded(result)
    assert installer.values()['KNOWE_PORT'] == '19001'
    assert '宿主机端口 19001' in result.stdout
    assert next(c for c in installer.calls() if 'up' in c['args'])['port'] == '19001'


def test_hanging_registry_pull_is_cancelled_and_mirror_is_saved(installer):
    result = installer.run(QA_UNCACHED='1', QA_OFFICIAL_PULL='hang', KNOWE_PULL_TIMEOUT='1')
    succeeded(result)
    assert installer.values()['KNOWE_NODE_IMAGE'].startswith('m.daocloud.io/')
    assert installer.values()['KNOWE_PYTHON_IMAGE'].startswith('m.daocloud.io/')
    assert installer.values()['KNOWE_NPM_REGISTRY'] == 'https://registry.npmmirror.com'
    assert '切换到' in result.stdout and '超过 1 秒' in result.stderr
    for pid in Path(installer.env['QA_PULL_PIDS']).read_text().splitlines():
        with pytest.raises(ProcessLookupError): os.kill(int(pid), 0)
    up = next(c for c in installer.calls() if 'up' in c['args'])
    assert up['node'] == installer.values()['KNOWE_NODE_IMAGE']


def test_custom_registry_and_quoted_settings_are_preserved(installer):
    installer.config('KNOWE_PORT="19234"\nKNOWE_NODE_IMAGE=private.example/node:22\n'
                     'KNOWE_PYTHON_IMAGE=private.example/python:3.11\n'
                     'KNOWE_NPM_REGISTRY=https://private.example/npm\n')
    succeeded(installer.run(KNOWE_DOWNLOAD_SOURCE='mirror'))
    assert installer.values()['KNOWE_PORT'] == '19234'
    assert installer.values()['KNOWE_NODE_IMAGE'] == 'private.example/node:22'
    assert installer.values()['KNOWE_NPM_REGISTRY'] == 'https://private.example/npm'
    assert not any('m.daocloud.io/' in str(c['args']) for c in installer.calls())


def test_all_registries_fail_without_build_or_start(installer):
    result = installer.run(QA_UNCACHED='1', QA_OFFICIAL_PULL='fail', QA_MIRROR_PULL='fail')
    assert result.returncode != 0
    assert '均下载失败' in result.stderr
    assert not any('build' in c['args'] or 'up' in c['args'] for c in installer.calls())


def test_official_only_does_not_switch_to_a_third_party(installer):
    result = installer.run(KNOWE_DOWNLOAD_SOURCE='official', QA_UNCACHED='1', QA_OFFICIAL_PULL='fail')
    assert result.returncode != 0
    assert not any('m.daocloud.io/' in str(c['args']) for c in installer.calls())


def test_failed_build_does_not_report_started(installer):
    result = installer.run(QA_BUILD_FAIL='1')
    assert result.returncode != 0
    assert '已启动' not in result.stdout
    assert not any('up' in c['args'] for c in installer.calls())


@pytest.mark.parametrize('port', ['0', '65536', 'bad', '-1'])
def test_invalid_port_stops_before_download(installer, port):
    result = installer.run(KNOWE_PORT=port)
    assert result.returncode != 0 and 'KNOWE_PORT' in result.stderr
    assert not any(c['args'][0] == 'pull' for c in installer.calls())


def test_does_not_replace_a_real_host_tcp_listener(installer):
    (installer.bin / 'ss').unlink()
    if not (shutil.which('ss') or shutil.which('lsof') or Path('/proc/net/tcp').exists()):
        pytest.skip('Host has no supported TCP listener inspection tool')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = installer.run(KNOWE_PORT=str(port))
        succeeded(result)
        assert port < int(installer.values()['KNOWE_PORT']) < port + 100


def test_termination_cleans_up_active_pull(installer):
    process = subprocess.Popen(['sh', str(installer.root / 'deploy/install.sh')],
                               cwd=installer.root, env={**installer.env, 'QA_UNCACHED':'1', 'QA_OFFICIAL_PULL':'hang'},
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        pids = Path(installer.env['QA_PULL_PIDS'])
        while not pids.exists():
            assert time.monotonic() < deadline, 'Installer did not start a pull'
            time.sleep(.02)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode == 143
        for pid in pids.read_text().splitlines():
            with pytest.raises(ProcessLookupError): os.kill(int(pid), 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
