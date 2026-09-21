import os

from agent.protocol.artifact import get_workspace_root
from agent.tools.scheduler.task_store import TaskStore
from common.utils import expand_path
from config import conf


def test_artifact_workspace_uses_project_default_when_config_is_missing(monkeypatch):
    monkeypatch.delitem(conf(), "agent_workspace", raising=False)

    assert get_workspace_root() == os.path.realpath(expand_path("./workspace"))


def test_scheduler_store_uses_project_default_when_path_is_omitted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store = TaskStore()
    assert store.store_path == os.path.realpath(
        tmp_path / "workspace" / "scheduler" / "tasks.json"
    )
