"""Exercise the updater with real local Git repositories and a stub deployer."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def updater(tmp_path):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', QA_INSTALL_LOG=str(tmp_path / 'install.log'))
    for key in list(environment):
        if key.startswith(('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')):
            environment.pop(key)

    def git(directory, *arguments):
        return subprocess.check_output(['git', '-C', str(directory), *arguments],
                                       env=environment, text=True, stderr=subprocess.PIPE).strip()

    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(origin)],
                   env=environment, check=True, capture_output=True)
    publisher = tmp_path / 'publisher'
    subprocess.run(['git', 'clone', str(origin), str(publisher)],
                   env=environment, check=True, capture_output=True)

    def identity(directory):
        git(directory, 'config', 'user.name', 'Updater Test')
        git(directory, 'config', 'user.email', 'updater@example.invalid')

    identity(publisher)
    shutil.copy2(ROOT / 'update.sh', publisher / 'update.sh')
    (publisher / 'deploy').mkdir()
    (publisher / 'deploy/install.sh').write_text(
        '#!/bin/sh\nset -eu\n'
        'printf "%s\\n" "$(pwd -P)" "$(git rev-parse HEAD)" "$@" > "$QA_INSTALL_LOG"\n'
        'exit "${QA_INSTALL_STATUS:-0}"\n'
    )
    (publisher / '.gitignore').write_text('.env\ndeploy/data/\ndeploy/workspaces/\ndeploy/web-password\n')
    (publisher / 'version.txt').write_text('original\n')
    git(publisher, 'add', '.')
    git(publisher, 'commit', '-m', 'Initial deployment')
    git(publisher, 'push', '-u', 'origin', 'main')
    checkout = tmp_path / 'checkout with spaces'
    subprocess.run(['git', 'clone', str(origin), str(checkout)],
                   env=environment, check=True, capture_output=True)
    identity(checkout)

    class Updater:
        root = checkout
        remote = publisher
        bare = origin
        log = Path(environment['QA_INSTALL_LOG'])

        def git(self, *arguments):
            return git(self.root, *arguments)

        def publish(self, *, replace_updater=False):
            (publisher / 'version.txt').write_text('updated\n')
            if replace_updater:
                path = publisher / 'update.sh'
                path.write_text('#!/bin/sh\n' + '# Changed updater layout\n' * 100 + path.read_text())
            with (publisher / 'deploy/install.sh').open('a') as output:
                output.write('# Updated deployer\n')
            git(publisher, 'add', '.')
            git(publisher, 'commit', '-m', 'Remote update')
            git(publisher, 'push')
            return git(publisher, 'rev-parse', 'HEAD')

        def run(self, *arguments, **changes):
            return subprocess.run(['sh', str(self.root / 'update.sh'), *arguments], cwd=tmp_path,
                                  env={**environment, **changes}, text=True, capture_output=True, timeout=15)

    return Updater()


def test_updates_from_any_directory_and_survives_its_own_replacement(updater):
    saved = {}
    for name in ['.env', 'deploy/web-password', 'deploy/data/chat.txt', 'deploy/workspaces/project.txt', 'personal.txt']:
        path = updater.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('existing contents: ' + name)
        saved[path] = path.read_bytes()
    revision = updater.publish(replace_updater=True)
    result = updater.run('--lan')
    assert result.returncode == 0, result.stdout + result.stderr
    assert updater.git('rev-parse', 'HEAD') == revision
    assert updater.log.read_text().splitlines() == [str(updater.root.resolve()), revision, '--lan']
    assert all(path.read_bytes() == content for path, content in saved.items())


def test_runs_deployer_even_when_source_is_already_up_to_date(updater):
    result = updater.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert updater.log.read_text().splitlines()[1] == updater.git('rev-parse', 'HEAD')


@pytest.mark.parametrize('staged', [False, True])
def test_preserves_local_source_changes_and_stops_before_deployment(updater, staged):
    before = updater.git('rev-parse', 'HEAD')
    updater.publish()
    path = updater.root / 'version.txt'
    path.write_text('local customization\n')
    if staged:
        updater.git('add', 'version.txt')
    result = updater.run()
    assert result.returncode != 0 and '未提交' in result.stderr
    assert updater.git('rev-parse', 'HEAD') == before
    assert path.read_text() == 'local customization\n'
    assert not updater.log.exists()


def test_pull_failure_never_invokes_deployer(updater):
    before = updater.git('rev-parse', 'HEAD')
    updater.git('remote', 'set-url', 'origin', str(updater.bare / 'missing'))
    result = updater.run()
    assert result.returncode != 0 and '源码拉取失败' in result.stderr
    assert updater.git('rev-parse', 'HEAD') == before
    assert not updater.log.exists()


def test_diverged_branch_is_not_merged_or_deployed(updater):
    (updater.root / 'local.txt').write_text('local commit')
    updater.git('add', 'local.txt')
    updater.git('commit', '-m', 'Local commit')
    before = updater.git('rev-parse', 'HEAD')
    updater.publish()
    result = updater.run()
    assert result.returncode != 0 and '源码拉取失败' in result.stderr
    assert updater.git('rev-parse', 'HEAD') == before
    assert not updater.log.exists()


@pytest.mark.parametrize('state', ['detached', 'no-upstream'])
def test_requires_a_branch_with_a_configured_upstream(updater, state):
    if state == 'detached':
        updater.git('checkout', '--detach')
    else:
        updater.git('branch', '--unset-upstream')
    result = updater.run()
    assert result.returncode != 0
    assert not updater.log.exists()


@pytest.mark.parametrize('argument', ['--help', '--unknown', '--no-build'])
def test_help_and_invalid_arguments_do_not_pull_or_deploy(updater, argument):
    before = updater.git('rev-parse', 'HEAD')
    updater.publish()
    result = updater.run(argument)
    assert (result.returncode == 0) == (argument == '--help')
    assert updater.git('rev-parse', 'HEAD') == before
    assert not updater.log.exists()


def test_propagates_deployment_failure(updater):
    revision = updater.publish()
    result = updater.run(QA_INSTALL_STATUS='7')
    assert result.returncode == 7
    assert updater.log.read_text().splitlines()[1] == revision
