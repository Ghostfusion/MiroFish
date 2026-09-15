from types import SimpleNamespace

from flask import Flask

from app.api import graph as graph_api
from app.api import report as report_api
from app.models.project import ProjectStatus
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import RunnerStatus
from app.utils.graph_lifecycle import (
    get_graph_readers,
    unregister_graph_reader,
)


def _json_result(result):
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, result.status_code
    return response.get_json(), status


def test_report_generation_waits_for_a_terminal_run_state(monkeypatch):
    simulation = SimpleNamespace(project_id="proj-1", graph_id="graph-1")
    monkeypatch.setattr(
        report_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation
        ),
    )
    monkeypatch.setattr(
        report_api.ReportManager,
        "get_report_by_simulation",
        classmethod(lambda _cls, _simulation_id: None),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                runner_status=RunnerStatus.STOPPING
            )
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/report/generate",
        method="POST",
        json={"simulation_id": "sim-1"},
    ):
        body, status = _json_result(report_api.generate_report())

    assert status == 409
    assert "still active" in body["error"]


def test_active_rerun_does_not_return_a_stale_completed_report(monkeypatch):
    simulation = SimpleNamespace(project_id="proj-1", graph_id="graph-1")
    monkeypatch.setattr(
        report_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation
        ),
    )
    monkeypatch.setattr(
        report_api.ReportManager,
        "get_report_by_simulation",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                report_id="old-report",
                status=report_api.ReportStatus.COMPLETED,
            )
        ),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                runner_status=RunnerStatus.STOPPING
            )
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/report/generate",
        method="POST",
        json={"simulation_id": "sim-1"},
    ):
        body, status = _json_result(report_api.generate_report())

    assert status == 409
    assert "still active" in body["error"]


def test_failed_run_cannot_generate_a_report(monkeypatch):
    simulation = SimpleNamespace(project_id="proj-1", graph_id="graph-1")
    monkeypatch.setattr(
        report_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation
        ),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                runner_status=RunnerStatus.FAILED
            )
        ),
    )
    app = Flask(__name__)
    with app.test_request_context(
        "/api/report/generate",
        method="POST",
        json={"simulation_id": "sim-1"},
    ):
        body, status = _json_result(report_api.generate_report())

    assert status == 409
    assert "successfully completed" in body["error"]


def test_report_reader_lease_blocks_graph_delete(monkeypatch):
    simulation = SimpleNamespace(
        simulation_id="sim-1",
        project_id="proj-1",
        graph_id="graph-1",
        status=SimulationStatus.READY,
    )
    project = SimpleNamespace(
        project_id="proj-1",
        graph_id="graph-1",
        status=ProjectStatus.GRAPH_COMPLETED,
        simulation_requirement="mock requirement",
    )
    run_state = SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
    worker_targets = []

    class Tasks:
        def create_task(self, **_kwargs):
            return "task-1"

        def update_task(self, *_args, **_kwargs):
            pass

        def complete_task(self, *_args, **_kwargs):
            pass

        def fail_task(self, *_args, **_kwargs):
            pass

    class ParkedThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            worker_targets.append(self.target)

    class Agent:
        def __init__(self, **_kwargs):
            pass

        def generate_report(self, *, progress_callback, report_id):
            progress_callback("mock", 100, "done")
            return SimpleNamespace(
                report_id=report_id,
                status=report_api.ReportStatus.COMPLETED,
                error=None,
            )

    monkeypatch.setattr(
        report_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation
        ),
    )
    monkeypatch.setattr(
        report_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        report_api.SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: run_state),
    )
    monkeypatch.setattr(
        report_api.ReportManager,
        "get_report_by_simulation",
        classmethod(lambda _cls, _simulation_id: None),
    )
    monkeypatch.setattr(
        report_api.ReportManager,
        "save_report",
        classmethod(lambda _cls, _report: None),
    )
    monkeypatch.setattr(report_api, "TaskManager", Tasks)
    monkeypatch.setattr(report_api, "ReportAgent", Agent)
    monkeypatch.setattr(report_api.threading, "Thread", ParkedThread)
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: []),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )

    app = Flask(__name__)
    report_id = None
    try:
        with app.test_request_context(
            "/api/report/generate",
            method="POST",
            json={"simulation_id": "sim-1"},
        ):
            body, status = _json_result(report_api.generate_report())
        assert status == 200
        report_id = body["data"]["report_id"]
        assert get_graph_readers("graph-1") == [report_id]
        assert len(worker_targets) == 1

        with app.test_request_context(
            "/api/graph/project/proj-1",
            method="DELETE",
        ):
            blocked, blocked_status = _json_result(graph_api.delete_project("proj-1"))

        assert blocked_status == 409
        assert f"report:{report_id}" in blocked["error"]

        # Let the parked background report finish; its finally block must
        # release the lease even if report generation fails.
        worker_targets[0]()
        assert get_graph_readers("graph-1") == []
    finally:
        if report_id:
            unregister_graph_reader("graph-1", report_id)
