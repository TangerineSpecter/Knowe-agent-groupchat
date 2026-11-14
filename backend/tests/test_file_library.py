from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from backend.file_library import FileLibrary
from backend.library_api import dispatch_library_http, sync
from backend.server import KnoweServer


def item(library, **kwargs):
    return library.listing(**kwargs)['items'][0]


def test_managed_file_trash_restore_and_release(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    root = tmp_path / 'project'
    root.mkdir()
    path = root / '报告.md'
    path.write_text('report')
    fid = lib.register(path, origin='generated', managed_root=root, project_id='p', project_name='Project', seq=12)
    row = item(lib)
    assert lib.listing()['storage']['active_bytes'] == 6
    with pytest.raises(ValueError, match='引用'):
        lib.action(fid, 'trash', version=row['version'])
    with pytest.raises(ValueError, match='任务'):
        lib.action(fid, 'trash', version=row['version'], confirm_references=True, busy_projects={'p'})
    lib.action(fid, 'trash', version=row['version'], confirm_references=True)
    assert not path.exists()
    assert lib.listing()['total'] == 0
    assert lib.listing(status='trash')['storage']['trash_bytes'] == 6
    with pytest.raises(FileNotFoundError):
        lib.resolve(fid)
    lib.action(fid, 'restore')
    assert path.read_text() == 'report'
    lib.action(fid, 'trash', version=item(lib)['version'], confirm_references=True)
    assert lib.action(fid, 'purge')['released_bytes'] == 6
    assert not path.exists()
    assert lib.listing(status='trash')['total'] == 0


def test_external_reference_never_deletes_original_or_resurrects_from_history(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    path = tmp_path / 'private.txt'
    path.write_text('private')
    fid = lib.register(path, origin='external', project_id='p')
    lib.action(fid, 'trash', confirm_references=True)
    assert path.read_text() == 'private'
    assert lib.register(path, origin='external', project_id='p') == fid
    assert lib.listing()['total'] == 0
    lib.action(fid, 'restore')
    lib.action(fid, 'trash', confirm_references=True)
    lib.action(fid, 'purge')
    assert path.read_text() == 'private'


def test_conflicts_changed_bytes_missing_and_symlinks(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    root = tmp_path / 'workspace'; root.mkdir()
    path = root / 'file.txt'; path.write_text('one')
    fid = lib.register(path, origin='generated', managed_root=root)
    version = item(lib)['version']
    path.write_text('updated bytes')
    with pytest.raises(ValueError, match='变化'):
        lib.action(fid, 'trash', version=version)
    lib.refresh()
    lib.action(fid, 'trash', version=item(lib)['version'])
    path.write_text('replacement')
    with pytest.raises(ValueError, match='同名'):
        lib.action(fid, 'restore')
    assert path.read_text() == 'replacement'
    path.unlink(); lib.action(fid, 'restore')
    assert path.read_text() == 'updated bytes'
    path.unlink(); lib.refresh()
    assert item(lib)['status'] == 'missing'
    path.write_text('new')
    with pytest.raises(ValueError, match='变化'):
        lib.action(fid, 'trash', version=version)
    path.unlink(); path.symlink_to(tmp_path / 'data' / 'library' / 'catalog.sqlite3')
    with pytest.raises(FileNotFoundError):
        lib.resolve(fid)
    with pytest.raises(ValueError, match='符号链接'):
        lib.register(path, origin='generated', managed_root=root)


def test_upload_dedup_references_and_discard(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    uploads = tmp_path / 'uploads'; uploads.mkdir()
    path = uploads / 'hello.txt'; path.write_text('hello')
    lib.sync_uploads(uploads); lib.sync_uploads(uploads)
    assert lib.listing(origin='unreferenced')['total'] == 1
    lib.record_event({'project_id': 'p', 'seq': 4, 'attachments': [{'path': str(path)}]},
                     project_root=None, project_name='p', upload_root=uploads, verify_attachment=lambda _: True)
    assert lib.listing(origin='unreferenced')['total'] == 0
    assert lib.discard_upload(path, uploads) is False
    other = uploads / 'unused.txt'; other.write_text('discard')
    lib.sync_uploads(uploads)
    assert lib.discard_upload(other, uploads)
    assert not other.exists() and path.exists()
    assert FileLibrary(tmp_path / 'data').lock is lib.lock


def test_crash_recovery_and_preview_without_live_engine(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    root = tmp_path / 'project'; root.mkdir()
    path = root / 'index.html'; path.write_text('<h1>Report</h1>')
    (root / 'style.css').write_text('h1 {color:red}')
    fid = lib.register(path, origin='generated', managed_root=root)
    server = KnoweServer(data_dir='')
    server._file_library = lib
    canonical, target, meta = server._resolve_preview_target('__library__', f'{fid}/index.html')
    assert canonical == '__library__' and target == path and meta['name'] == 'index.html'
    assert server._resolve_preview_target('__library__', f'{fid}/style.css')[1] == root / 'style.css'
    with pytest.raises(ValueError):
        server._resolve_preview_target('__library__', f'{fid}/../data/library/catalog.sqlite3')
    with lib.connect() as db:
        db.execute("UPDATE files SET status='trashing',trash_path=? WHERE id=?", (str(lib.trash / fid), fid))
    path.rename(lib.trash / fid)
    recovered = FileLibrary(tmp_path / 'data')
    assert item(recovered, status='trash')['id'] == fid
    recovered.action(fid, 'restore')
    assert path.exists()


@pytest.mark.asyncio
async def test_legacy_events_imported_and_batch_failures_are_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv('KNOWE_WEB_WORKSPACE_ROOT', raising=False)
    root = tmp_path / 'project'; root.mkdir()
    (root / 'story.md').write_text('story')
    events = [{'seq': 7, 'agent_id': 'writer', 'files': [{'path': 'story.md'}]}]
    project = SimpleNamespace(name='Writing', ring=SimpleNamespace(events=lambda: []), members=[])
    server = SimpleNamespace(data_root=tmp_path / 'data', engines={}, project_dirs={'p': str(root)},
        store=SimpleNamespace(load_all_events=lambda pid: events), hub=SimpleNamespace(projects={'p': project}))
    sync(server); sync(server)
    response = await dispatch_library_http(server, 'GET', '/api/library', 'query=story', b'')
    assert response.status == 200
    row = response.body['items'][0]
    assert len(row['references']) == 1 and row['references'][0]['seq'] == 7
    response = await dispatch_library_http(server, 'POST', '/api/library/action', '', json.dumps({
        'action': 'trash', 'confirm_references': True, 'files': [{'id': row['id'], 'version': row['version']}, {'id': 'absent'}],
    }).encode())
    assert len(response.body['succeeded']) == 1 and len(response.body['failed']) == 1


def test_external_os_alias_is_canonical_and_purge_does_not_resurrect(tmp_path):
    lib = FileLibrary(tmp_path / 'data')
    directory = tmp_path / 'documents'; directory.mkdir()
    path = directory / 'original.txt'; path.write_text('original')
    alias = tmp_path / 'alias'; alias.symlink_to(directory, target_is_directory=True)
    fid = lib.register(alias / path.name, origin='external', project_id='p')
    assert lib.resolve(fid)[0] == path
    lib.action(fid, 'trash', confirm_references=True)
    lib.action(fid, 'purge')
    assert path.read_text() == 'original'
    assert lib.register(path, origin='external', project_id='p') == fid
    assert lib.listing()['total'] == 0
