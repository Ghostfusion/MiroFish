"""图谱构建链路：逐块写入本地图谱、进度推进、任务结果带节点/边计数。"""

import threading
import time
from datetime import datetime

import pytest
from flask import Flask

from app.api import graph as graph_api
from app.models.project import Project, ProjectStatus
from app.models.task import TaskStatus
from app.services import graph_builder as graph_builder_module
from app.services.text_processor import TextProcessor

SOURCE_TEXT = "社区活动讨论帖子正文，包含人物与话题。" * 4
CHUNK_SIZE = 20
CHUNK_OVERLAP = 0
ONTOLOGY = {
    "entity_types": [{"name": "Speaker", "attributes": ["role"]}],
    "edge_types": [{
        "name": "MENTIONS",
        "attributes": ["reason"],
        "source_targets": [{"source": "Speaker", "target": "Speaker"}],
    }],
}
GRAPH_NODES = [
    {"uuid": "node-1", "name": "甲", "labels": ["Entity", "Speaker"],
     "summary": "", "attributes": {}},
    {"uuid": "node-2", "name": "乙", "labels": ["Entity", "Speaker"],
     "summary": "", "attributes": {}},
]
GRAPH_EDGES = [
    {"uuid": "edge-1", "name": "MENTIONS", "fact": "甲提到乙",
     "source_node_uuid": "node-1", "target_node_uuid": "node-2",
     "attributes": {}, "episodes": []},
    {"uuid": "edge-2", "name": "MENTIONS", "fact": "乙提到甲",
     "source_node_uuid": "node-2", "target_node_uuid": "node-1",
     "attributes": {}, "episodes": []},
    {"uuid": "edge-3", "name": "MENTIONS", "fact": "甲提到自己",
     "source_node_uuid": "node-1", "target_node_uuid": "node-1",
     "attributes": {}, "episodes": []},
]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """构建前的配置校验与本用例目标无关，固定为通过以保持用例自洽。"""

    monkeypatch.setattr(graph_api.Config, "validate", classmethod(lambda _cls: []))


def _project():
    now = datetime.now().isoformat()
    return Project(
        project_id="proj-1",
        name="Project",
        status=ProjectStatus.ONTOLOGY_GENERATED,
        created_at=now,
        updated_at=now,
        ontology=ONTOLOGY,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )


def _json_result(result):
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, result.status_code
    return response.get_json(), status


def test_graph_build_writes_every_chunk_once_and_reports_graph_counts(monkeypatch):
    project = _project()
    ingested = []

    def fake_add_episode(**kwargs):
        ingested.append(kwargs)
        return {
            "episode": {"uuid": f"episode-{len(ingested)}"},
            "node_count": 1,
            "edge_count": 1,
            "node_uuids": [],
            "edge_uuids": [],
        }

    monkeypatch.setattr(graph_builder_module, "add_episode", fake_add_episode)
    monkeypatch.setattr(
        graph_builder_module, "fetch_nodes", lambda _graph_id: list(GRAPH_NODES)
    )
    monkeypatch.setattr(
        graph_builder_module, "fetch_edges", lambda _graph_id: list(GRAPH_EDGES)
    )

    def forbidden_sleep(*_args, **_kwargs):
        raise AssertionError("图谱写库是同步的，构建链路不允许 sleep/轮询")

    monkeypatch.setattr(time, "sleep", forbidden_sleep)

    updates = []

    class Tasks:
        def create_task(self, _description):
            return "task-1"

        def get_task(self, _task_id):
            return None

        def update_task(self, _task_id, **kwargs):
            updates.append(kwargs)

        def fail_task(self, *_args, **_kwargs):
            raise AssertionError("图谱构建不应进入失败分支")

    workers = []

    real_thread_maker = threading.Thread

    class Thread:
        """threading.Thread 替身：后台构建线程交给测试显式驱动，其余线程照常启动。

        threading.Thread 是被全局替换的，因此并发写入用的线程池也会经过这里：
        构建线程只记录不启动（由测试调用 workers[0]() 决定时机），线程池线程则真正启动，
        否则池里的任务永远不会执行，future.result() 会一直等下去。
        """

        def __init__(self, *, target=None, daemon=False, name=None, args=(), kwargs=None):
            self._build_worker = bool(daemon)
            if self._build_worker:
                workers.append(target)
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self._thread = None

        def start(self):
            if self._build_worker or self._target is None:
                return
            self._thread = real_thread_maker(
                target=self._target, args=self._args, kwargs=self._kwargs, daemon=True
            )
            self._thread.start()

        def join(self, timeout=None):
            if self._thread is not None:
                self._thread.join(timeout)

    monkeypatch.setattr(graph_api, "TaskManager", Tasks)
    monkeypatch.setattr(
        graph_api, "GraphBuilderService", graph_builder_module.GraphBuilderService
    )
    monkeypatch.setattr(graph_api.threading, "Thread", Thread)
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_extracted_text",
        classmethod(lambda _cls, _project_id: SOURCE_TEXT),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={
            "project_id": "proj-1",
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
        },
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 200
    assert body["success"] is True
    assert len(workers) == 1

    workers[0]()

    chunks = TextProcessor.split_text(
        SOURCE_TEXT, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP
    )
    assert len(chunks) > 1
    # 文本块是并发写入的，因此断言"每块恰好写一次"而不是写入顺序。
    assert len(ingested) == len(chunks)
    assert sorted(call["body"] for call in ingested) == sorted(chunks)
    assert sorted(call["name"] for call in ingested) == sorted(
        f"chunk {index}/{len(chunks)}" for index in range(1, len(chunks) + 1)
    )
    assert {call["group_id"] for call in ingested} == {project.graph_id}

    progress_values = [u["progress"] for u in updates if "progress" in u]
    assert progress_values == sorted(progress_values)
    assert progress_values[-1] == 100

    result = updates[-1]["result"]
    assert updates[-1]["status"] == TaskStatus.COMPLETED
    assert result["project_id"] == "proj-1"
    assert result["graph_id"] == project.graph_id
    assert result["node_count"] == len(GRAPH_NODES)
    assert result["edge_count"] == len(GRAPH_EDGES)
    assert result["chunk_count"] == len(chunks)
    assert project.status == ProjectStatus.GRAPH_COMPLETED
