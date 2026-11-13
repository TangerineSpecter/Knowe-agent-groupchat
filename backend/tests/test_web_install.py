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
if args[:3] == ['compose','--progress','plain'] and args[-2:] == ['pull','base-image']:
    args=['pull',os.environ['KNOWE_INSTALL_PULL_IMAGE']]
root=pathlib.Path.cwd()
with open(os.environ['QA_LOG'], 'a') as output:
    output.write(json.dumps({'args':args, 'port':os.environ.get('KNOWE_PORT'),
        'bind':os.environ.get('KNOWE_BIND_HOST'),
        'node':os.environ.get('KNOWE_NODE_IMAGE'), 'python':os.environ.get('KNOWE_PYTHON_IMAGE'),
        'pip':os.environ.get('KNOWE_PIP_INDEX_URL'), 'apt':os.environ.get('KNOWE_APT_MIRROR'),
        'browser':os.environ.get('KNOWE_PLAYWRIGHT_DOWNLOAD_HOST')})+'\n')
if args == ['info']:
    sys.exit(int(os.environ.get('QA_DAEMON_FAIL','0')))
if args[:2] == ['compose','version']:
    sys.exit(0)
if args[:3] == ['compose','config','--images']:
    print('knowe-web:local')
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
    print('' if (root/'runtime-stopped').exists() else os.environ.get('QA_OWN_BINDING',''))
    sys.exit(0)
if args[:2] == ['compose','stop']:
    assert args[-1]=='knowe'
    (root/'runtime-stopped').touch()
    sys.exit(0)
if args[:2] == ['ps','--format']:
    for port in os.environ.get('QA_DOCKER_PORTS','').split(','):
        if (root/'runtime-stopped').exists() and os.environ.get('QA_OWN_BINDING','').endswith(':'+port): continue
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
    if mode in ('progress', 'progress-then-stall'):
        for size in range(12):
            print('layer Downloading '+str(size)+'MB/12MB', flush=True)
            time.sleep(.25)
        if mode == 'progress-then-stall':
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
    if pathlib.Path('runtime-stopped').exists() and os.environ.get('QA_OWN_BINDING','').endswith(':'+port): continue
    if port: print('LISTEN 0 128 [::]:'+port+' [::]:*')
'''
MOCK_IP = r'''
import os
print('2: ens33 inet '+os.environ.get('QA_LAN_IP','10.20.30.40')+'/24 scope global ens33')
'''


@pytest.fixture
def installer(tmp_path):
    directory = tmp_path / 'checkout with spaces'
    (directory / 'deploy').mkdir(parents=True)
    shutil.copy2(ROOT / 'deploy/install.sh', directory / 'deploy/install.sh')
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name, source in {'docker': MOCK_DOCKER, 'ss': MOCK_SS, 'ip': MOCK_IP}.items():
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

        def run(self, *arguments, **changes):
            return subprocess.run(['sh', str(self.root / 'deploy/install.sh'), *arguments], cwd=tmp_path,
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
    assert installer.values()['KNOWE_BIND_HOST'] == '0.0.0.0'
    assert 'http://127.0.0.1:18090' in result.stdout
    assert 'http://10.20.30.40:18090' in result.stdout
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
    installer.config('KNOWE_PORT=8090\nKNOWE_BIND_HOST=127.0.0.1\n')
    state = installer.root / 'deploy/data/web'
    state.mkdir(parents=True)
    (state / 'session.key').write_text('persisted web instance')
    changes = {'QA_OWN_CONTAINER': 'existing-knowe', 'QA_OWN_BINDING': '127.0.0.1:8090',
               'QA_DOCKER_PORTS': '8090', 'QA_NATIVE_PORTS': '8090'} if has_container else {}
    succeeded(installer.run(**changes))
    assert installer.values()['KNOWE_PORT'] == '8090'


def test_existing_loopback_deployment_switches_to_lan_without_build_or_port_change(installer):
    installer.config('KNOWE_PORT=18090\nKNOWE_BIND_HOST=127.0.0.1\n')
    password = installer.root / 'deploy/web-password'
    password.write_text('keep-existing-password')
    result = installer.run('--lan', '--no-build', QA_OWN_CONTAINER='existing-knowe',
                           QA_OWN_BINDING='127.0.0.1:18090', QA_DOCKER_PORTS='18090', QA_NATIVE_PORTS='18090')
    succeeded(result)
    assert installer.values()['KNOWE_BIND_HOST'] == '0.0.0.0'
    assert installer.values()['KNOWE_PORT'] == '18090'
    assert password.read_text() == 'keep-existing-password'
    assert 'http://10.20.30.40:18090' in result.stdout
    calls = installer.calls()
    assert not any(c['args'][0]=='pull' or 'build' in c['args'] for c in calls)
    assert any(c['args'][:2]==['compose','stop'] for c in calls)
    up = next(c for c in calls if c['args'][:2]==['compose','up'])
    assert up['bind']=='0.0.0.0' and up['port']=='18090'
    assert '--no-build' in up['args'] and 'never' in up['args']


@pytest.mark.parametrize('arguments,old_bind,expected', [
    ([], '127.0.0.1', '127.0.0.1'),
    (['--local'], '0.0.0.0', '127.0.0.1'),
    ([], '192.168.5.113', '192.168.5.113'),
])
def test_local_and_custom_bindings_remain_explicit_choices(installer, arguments, old_bind, expected):
    installer.config('KNOWE_BIND_HOST='+old_bind+'\n')
    result = installer.run(*arguments, '--no-build')
    succeeded(result)
    assert installer.values()['KNOWE_BIND_HOST'] == expected
    assert f'http://{expected}:18090' in result.stdout
    assert 'http://10.20.30.40' not in result.stdout


def test_no_build_missing_image_does_not_stop_the_running_service(installer):
    result = installer.run('--lan', '--no-build', QA_UNCACHED='1', QA_OWN_CONTAINER='existing-knowe',
                           QA_OWN_BINDING='127.0.0.1:18090')
    assert result.returncode != 0 and '未找到已构建' in result.stderr
    assert not any(c['args'][0]=='pull' or 'stop' in c['args'] or 'build' in c['args'] for c in installer.calls())


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


def test_default_prefers_mirror_over_previously_selected_official_images(installer):
    installer.config('KNOWE_NODE_IMAGE=node:22-bookworm-slim\n'
                     'KNOWE_PYTHON_IMAGE=python:3.11-slim-bookworm\n')
    succeeded(installer.run(QA_UNCACHED='1'))
    pulls = [c['args'][1] for c in installer.calls() if c['args'][0] == 'pull']
    assert len(pulls) == 2 and all(image.startswith('m.daocloud.io/') for image in pulls)
    assert installer.values()['KNOWE_NODE_IMAGE'] == pulls[0]
    assert installer.values()['KNOWE_NPM_REGISTRY'] == 'https://registry.npmmirror.com'


def test_default_falls_back_to_official_when_mirror_fails(installer):
    succeeded(installer.run(QA_UNCACHED='1', QA_MIRROR_PULL='fail'))
    pulls = [c['args'][1] for c in installer.calls() if c['args'][0] == 'pull']
    assert pulls == ['m.daocloud.io/docker.io/library/node:22-bookworm-slim', 'node:22-bookworm-slim',
                     'm.daocloud.io/docker.io/library/python:3.11-slim-bookworm', 'python:3.11-slim-bookworm']
    assert installer.values()['KNOWE_NODE_IMAGE'] == 'node:22-bookworm-slim'


def test_all_build_download_sources_are_mirrored_and_passed_to_compose(installer):
    succeeded(installer.run())
    values = installer.values()
    assert values['KNOWE_PIP_INDEX_URL'] == 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple'
    assert values['KNOWE_APT_MIRROR'] == 'https://mirrors.tuna.tsinghua.edu.cn'
    assert values['KNOWE_PLAYWRIGHT_DOWNLOAD_HOST'] == 'https://cdn.npmmirror.com/binaries/playwright'
    build = next(c for c in installer.calls() if 'build' in c['args'])
    assert build['pip'] == values['KNOWE_PIP_INDEX_URL']
    assert build['apt'] == values['KNOWE_APT_MIRROR']
    assert build['browser'] == values['KNOWE_PLAYWRIGHT_DOWNLOAD_HOST']


def test_official_mode_uses_official_package_and_browser_sources(installer):
    succeeded(installer.run(KNOWE_DOWNLOAD_SOURCE='official'))
    build = next(c for c in installer.calls() if 'build' in c['args'])
    assert build['pip'] == 'https://pypi.org/simple'
    assert build['apt'] == 'https://deb.debian.org'
    assert build['browser'] == ''


def test_existing_download_sources_and_empty_browser_host_are_preserved(installer):
    installer.config('KNOWE_PIP_INDEX_URL=https://private.example/simple\n'
                     'KNOWE_APT_MIRROR=https://private.example/debian-mirror\n'
                     'KNOWE_PLAYWRIGHT_DOWNLOAD_HOST=\n')
    succeeded(installer.run())
    build = next(c for c in installer.calls() if 'build' in c['args'])
    assert build['pip'] == 'https://private.example/simple'
    assert build['apt'] == 'https://private.example/debian-mirror'
    assert build['browser'] == ''


def test_hanging_registry_pull_is_cancelled_and_mirror_is_saved(installer):
    result = installer.run(QA_UNCACHED='1', QA_OFFICIAL_PULL='hang', KNOWE_PULL_TIMEOUT='1',
                           KNOWE_DOWNLOAD_SOURCE='auto')
    succeeded(result)
    assert installer.values()['KNOWE_NODE_IMAGE'].startswith('m.daocloud.io/')
    assert installer.values()['KNOWE_PYTHON_IMAGE'].startswith('m.daocloud.io/')
    assert installer.values()['KNOWE_NPM_REGISTRY'] == 'https://registry.npmmirror.com'
    assert '切换到' in result.stdout and '连续 1 秒没有新的镜像拉取进度' in result.stderr
    for pid in Path(installer.env['QA_PULL_PIDS']).read_text().splitlines():
        with pytest.raises(ProcessLookupError): os.kill(int(pid), 0)
    up = next(c for c in installer.calls() if 'up' in c['args'])
    assert up['node'] == installer.values()['KNOWE_NODE_IMAGE']


def test_continuous_download_progress_can_exceed_the_idle_timeout(installer):
    started = time.monotonic()
    result = installer.run(QA_UNCACHED='1', QA_MIRROR_PULL='progress', KNOWE_PULL_TIMEOUT='1')
    succeeded(result)
    assert time.monotonic() - started > 6
    assert '11MB/12MB' in result.stdout
    assert '停止本次拉取' not in result.stderr and '切换到' not in result.stdout
    assert all(c['args'][1].startswith('m.daocloud.io/') for c in installer.calls() if c['args'][0] == 'pull')


def test_idle_deadline_starts_after_the_last_progress_update(installer):
    result = installer.run(QA_UNCACHED='1', QA_MIRROR_PULL='progress-then-stall', KNOWE_PULL_TIMEOUT='1')
    succeeded(result)
    assert '11MB/12MB' in result.stdout and '停止本次拉取' in result.stderr
    assert installer.values()['KNOWE_NODE_IMAGE'] == 'node:22-bookworm-slim'
    for pid in Path(installer.env['QA_PULL_PIDS']).read_text().splitlines():
        with pytest.raises(ProcessLookupError): os.kill(int(pid), 0)


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
                               cwd=installer.root, env={**installer.env, 'QA_UNCACHED':'1', 'QA_OFFICIAL_PULL':'hang',
                                                        'KNOWE_DOWNLOAD_SOURCE':'auto'},
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
