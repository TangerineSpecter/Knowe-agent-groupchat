from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

from backend.config import CONFIG
from backend import skill_installer as skills
from backend.extensions_api import dispatch_extensions_http
from backend.knowledge_api import dispatch_knowledge_http
from backend.knowledge_assets import KnowledgeAssetManager
from backend.knowledge_graph import KnowledgeGraphManager


@pytest.fixture
def environment(tmp_path, monkeypatch):
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    object.__setattr__(CONFIG, "skill_third_party_dir", str(tmp_path / "skills"))
    object.__setattr__(CONFIG, "skill_system_dirs", "")
    return tmp_path


def archive(files: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for path, body in files.items():
            output.writestr(path, body)
    return stream.getvalue()


def package(body="First version"):
    return archive({"repo/report/SKILL.md": "---\nname: report\ndescription: |\n  Make a report\n  with evidence\n---\n" + body,
                    "repo/report/scripts/run.py": "print('ok')\n",
                    "repo/report/references/example.md": "example"})


def test_installs_resources_and_reads_without_a_running_project(environment):
    preview = skills.prepare_archive(package())
    assert preview["skills"][0]["description"] == "Make a report\nwith evidence"
    result = skills.install(preview["token"], ["repo/report"])
    assert result["installed"] == ["report"]
    assert (skills.skill_root() / "report/scripts/run.py").read_text() == "print('ok')\n"
    assert not (skills.staging_root() / preview["token"]).exists()


@pytest.mark.asyncio
async def test_http_import_list_lifecycle_without_projects(environment):
    result = await dispatch_extensions_http("POST", "/api/extensions/skills/prepare",
        json.dumps({"archive_base64": base64.b64encode(package()).decode()}).encode())
    assert result.status == 200
    result = await dispatch_extensions_http("POST", "/api/extensions/skills/install",
        json.dumps({"token": result.body["token"], "selected": ["repo/report"]}).encode())
    assert result.status == 200
    result = await dispatch_knowledge_http("GET", "/api/knowledge/skillpacks", "", b"")
    row = next(row for row in result.body["third_party"] if row["name"] == "report")
    assert row["description"] == "Make a report with evidence"
    pack_id = row["pack_id"]
    response = await dispatch_knowledge_http("POST", f"/api/knowledge/skillpacks/{pack_id}", "",
                                            b'{"status":"retired"}')
    assert response.status == 200
    response = await dispatch_knowledge_http("POST", f"/api/knowledge/skillpacks/{pack_id}/purge", "", b'{}')
    assert response.status == 200
    assert not (skills.skill_root() / "report").exists()


def test_replacement_requires_explicit_choice_and_preserves_retired_state(environment):
    first = skills.prepare_archive(package())
    skills.install(first["token"], ["repo/report"])
    second = skills.prepare_archive(package("second"))
    assert second["skills"][0]["exists"]
    with pytest.raises(ValueError, match="已安装"):
        skills.install(second["token"], ["repo/report"])
    assert "First version" in (skills.skill_root() / "report/SKILL.md").read_text()
    skills.install(second["token"], ["repo/report"], replace=True)
    assert "second" in (skills.skill_root() / "report/SKILL.md").read_text()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "repo/../../escape", "C:/escape", "repo\\escape"])
def test_zip_traversal_rejected(environment, path):
    with pytest.raises(ValueError, match="不安全"):
        skills.prepare_archive(archive({path: "bad"}))
    assert list(skills.staging_root().iterdir()) == []


def test_links_and_expansion_bombs_rejected(environment, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        link = zipfile.ZipInfo("repo/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "/etc/passwd")
    with pytest.raises(ValueError, match="不安全"):
        skills.prepare_archive(stream.getvalue())
    monkeypatch.setattr(skills, "MAX_EXPANDED", 10)
    with pytest.raises(ValueError, match="过大"):
        skills.prepare_archive(package())


def test_invalid_selection_cannot_copy_unlisted_paths(environment):
    preview = skills.prepare_archive(package())
    with pytest.raises(ValueError, match="无效"):
        skills.install(preview["token"], ["../content"])
    with pytest.raises(ValueError, match="无效"):
        skills.install(preview["token"], ["repo/report", "repo/report"])


def test_materialized_resource_snapshot_and_retired_gate(environment):
    preview = skills.prepare_archive(package())
    skills.install(preview["token"], ["repo/report"])
    manager = KnowledgeAssetManager(KnowledgeGraphManager(environment / "data"))
    row = manager.list_skillpacks("p", environment / "internal")["third_party"][0]
    workspace = environment / "workspace"
    workspace.mkdir()
    result = manager.read_skillpack("p", environment / "internal", row["pack_id"], active_only=True, workspace=workspace)
    assert (workspace / result["resource_directory"] / "scripts/run.py").is_file()
    assert result["resource_directory"].startswith(".knowe-skills/")
    again = manager.read_skillpack("p", environment / "internal", row["pack_id"], active_only=True, workspace=workspace)
    assert again["resource_directory"] == result["resource_directory"]


def test_skill_resources_are_not_automatic_deliverables(environment):
    from backend.engine import ProjectEngine
    from backend.hub import Hub
    from backend.knowe_harness.completion import SQLiteCompletionStore
    from types import SimpleNamespace

    source = environment / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text("Instructions")
    (source / "template.md").write_text("Template")
    workspace = environment / "workspace"
    workspace.mkdir()
    engine = ProjectEngine(Hub(), "p", agent=None, workspace_root=workspace)
    envelope = SimpleNamespace(scope_root=str(workspace))
    baseline = engine._snapshot_workspace_baseline(envelope)
    skills.materialize(source, workspace, "skill")
    (workspace / "report.md").write_text("Actual deliverable")
    rows = SQLiteCompletionStore._scan_workspace_deltas("task", workspace, baseline or ())
    assert [row["path"] for row in rows] == ["report.md"]
    assert engine._snapshot_workspace_baseline(envelope) == ("report.md",)


def test_platform_rejects_skill_execution_with_actionable_message():
    from types import SimpleNamespace
    from backend.skill_tools import selected_skill_context

    engine = SimpleNamespace(project_id="__platform__")
    assert selected_skill_context(engine, []) == ""
    with pytest.raises(ValueError, match="项目群聊或成员私聊"):
        selected_skill_context(engine, ["system:project-report"])


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://example.com/a.zip", "https://github.com@localhost/a/b", "file:///etc/passwd"])
async def test_remote_installer_rejects_other_origins(environment, url):
    with pytest.raises(ValueError, match="GitHub"):
        await skills.prepare({"url": url})


@pytest.mark.asyncio
async def test_explicit_selection_and_worker_handoff_preserve_skill_resources(environment):
    from backend.engine import ProjectEngine, _TURN_SKILL_CONTEXT
    from backend.hub import Hub
    from backend.skill_tools import selected_skill_context
    from backend.tools_knowe import build_worker_registry
    preview = skills.prepare_archive(package())
    skills.install(preview['token'], ['repo/report'])
    root = environment / 'project'; root.mkdir()
    engine = ProjectEngine(Hub(), 'p', agent=None, workspace_root=root)
    engine._roster = {'writer_1': '技术写作'}
    manager = KnowledgeAssetManager(KnowledgeGraphManager(environment / 'data'))
    engine._assets = manager
    registry = build_worker_registry(engine, 'writer_1')
    available = json.loads(await registry.execute('list_skillpacks', {'query': 'report'}))['skills']
    pack_id = next(row['pack_id'] for row in available if row['name'] == 'report')
    read = json.loads(await registry.execute('read_skillpack', {'pack_id': pack_id}))
    assert read['found'] and (root / read['resource_directory'] / 'scripts/run.py').is_file()
    context = selected_skill_context(engine, [pack_id])
    assert 'First version' in context
    token = _TURN_SKILL_CONTEXT.set(context)
    try:
        engine.commit_handoff_step(target_id='writer_1', instruction='Write a report', decision='approved', keyword='Report')
    finally:
        _TURN_SKILL_CONTEXT.reset(token)
    envelopes = list((engine.internal_workspace / 'runtime/task-envelopes').rglob('*.json'))
    assert envelopes and context in json.loads(envelopes[0].read_text())["goal"]
    changed = await manager.set_skillpack_status('p', engine.internal_workspace, pack_id, 'retired')
    assert changed['ok']
    with pytest.raises(ValueError, match='停用'):
        selected_skill_context(engine, [pack_id])
    assert not json.loads(await registry.execute('read_skillpack', {'pack_id': pack_id}))['found']
    assert not any(row['pack_id'] == pack_id for row in json.loads(await registry.execute('list_skillpacks', {}))['skills'])
    # Reinstalling preserves the user's lifecycle decision.
    update = skills.prepare_archive(package('updated'))
    skills.install(update['token'], ['repo/report'], replace=True)
    assert not engine.read_skillpack(pack_id)['found']
