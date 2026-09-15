from types import SimpleNamespace
import json

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services import simulation_runner as runner_module
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
    SimulationStopPending,
)


def test_platform_completion_does_not_publish_terminal_success_before_barrier(
    monkeypatch, tmp_path
):
    simulation_id = "sim-1"
    sim_dir = tmp_path / simulation_id / "twitter"
    sim_dir.mkdir(parents=True)
    log_path = sim_dir / "actions.jsonl"
    log_path.write_text(
        '{"event_type":"simulation_end","total_rounds":1,"total_actions":0}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
        twitter_running=True,
    )

    SimulationRunner._read_action_log(str(log_path), 0, state, "twitter")

    assert state.twitter_completed is True
    assert state.runner_status == RunnerStatus.RUNNING


def test_manual_stop_timeout_leaves_monitor_owned_state_stopping(monkeypatch):
    state = SimulationRunState(
        simulation_id="sim-timeout",
        runner_status=RunnerStatus.RUNNING,
    )

    class Monitor:
        def join(self, timeout):
            assert timeout >= 30

        def is_alive(self):
            return True

    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    SimulationRunner._monitor_threads["sim-timeout"] = Monitor()
    SimulationRunner._processes.pop("sim-timeout", None)

    try:
        with pytest.raises(TimeoutError, match="仍在停止中"):
            SimulationRunner.stop_simulation("sim-timeout")
        assert state.runner_status == RunnerStatus.STOPPING
    finally:
        SimulationRunner._monitor_threads.pop("sim-timeout", None)
        SimulationRunner._manual_stop_requests.discard("sim-timeout")


def test_stop_api_keeps_pending_finalization_out_of_failed_state(monkeypatch):
    simulation = SimpleNamespace(status=SimulationStatus.STOPPING, error=None)
    saved = []
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "stop_simulation",
        classmethod(
            lambda _cls, _simulation_id: (_ for _ in ()).throw(
                SimulationStopPending("still draining")
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation,
            _save_simulation_state=lambda state: saved.append(state.status),
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/stop",
        method="POST",
        json={"simulation_id": "sim-pending"},
    ):
        response, status = simulation_api.stop_simulation()

    assert status == 202
    assert response.get_json()["pending"] is True
    assert simulation.status == SimulationStatus.STOPPING
    assert saved == []


def test_simulation_start_rejects_string_booleans():
    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/start",
        method="POST",
        json={"simulation_id": "sim-1", "force": "false"},
    ):
        response, status = simulation_api.start_simulation()

    assert status == 400
    assert "JSON boolean" in response.get_json()["error"]


def test_force_restart_does_not_continue_while_the_previous_run_is_finalizing(monkeypatch):
    simulation = SimpleNamespace(
        simulation_id="sim-1",
        project_id="proj-1",
        graph_id="graph-1",
        status=SimulationStatus.STOPPING,
    )
    cleanup_called = []
    monkeypatch.setattr(
        simulation_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _simulation_id: simulation,
            _save_simulation_state=lambda _state: None,
        ),
    )
    monkeypatch.setattr(
        simulation_api,
        "_check_simulation_prepared",
        lambda _simulation_id: (True, {}),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _simulation_id: SimpleNamespace(
                runner_status=RunnerStatus.STOPPING
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "stop_simulation",
        classmethod(
            lambda _cls, _simulation_id: (_ for _ in ()).throw(
                SimulationStopPending("still draining")
            )
        ),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "cleanup_simulation_logs",
        classmethod(
            lambda _cls, _simulation_id: cleanup_called.append(True)
        ),
    )
    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/start",
        method="POST",
        json={"simulation_id": "sim-1", "force": True},
    ):
        response, status = simulation_api.start_simulation()

    assert status == 409
    assert response.get_json()["pending"] is True
    assert cleanup_called == []


def test_monitor_start_failure_terminates_the_spawned_process(monkeypatch, tmp_path):
    simulation_id = "sim-start-failure"
    sim_dir = tmp_path / "runs" / simulation_id
    scripts_dir = tmp_path / "scripts"
    sim_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    (sim_dir / "simulation_config.json").write_text(
        json.dumps({
            "time_config": {
                "total_simulation_hours": 1,
                "minutes_per_round": 60,
            }
        }),
        encoding="utf-8",
    )
    (scripts_dir / "run_twitter_simulation.py").write_text("pass\n", encoding="utf-8")

    class Process:
        pid = 123

        def poll(self):
            return None

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("monitor failed")

    terminated = []
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(SimulationRunner, "SCRIPTS_DIR", str(scripts_dir))
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(runner_module.threading, "Thread", BrokenThread)
    monkeypatch.setattr(
        SimulationRunner,
        "_terminate_process",
        classmethod(lambda _cls, _process, sim_id: terminated.append(sim_id)),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, *_args, **_kwargs: None),
    )

    try:
        with pytest.raises(RuntimeError, match="monitor failed"):
            SimulationRunner.start_simulation(
                simulation_id,
                platform="twitter",
            )
        assert terminated == [simulation_id]
        assert simulation_id not in SimulationRunner._processes
        assert simulation_id not in SimulationRunner._action_queues
        assert simulation_id not in SimulationRunner._stdout_files
    finally:
        SimulationRunner._run_states.pop(simulation_id, None)
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._action_queues.pop(simulation_id, None)
        SimulationRunner._stdout_files.pop(simulation_id, None)
        SimulationRunner._stderr_files.pop(simulation_id, None)


def test_shutdown_terminates_producer_before_tail_read(monkeypatch):
    simulation_id = "sim-shutdown-order"
    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
    )
    events = []

    class Process:
        pid = 123
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

    process = Process()

    class Monitor:
        alive = True

        def join(self, timeout):
            assert timeout >= 30
            events.append("tail-read")
            state.runner_status = RunnerStatus.STOPPED
            self.alive = False

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(
        SimulationRunner,
        "get_run_state",
        classmethod(lambda _cls, _simulation_id: state),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_save_run_state",
        classmethod(lambda _cls, _state: None),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "_terminate_process",
        classmethod(
            lambda _cls, proc, _simulation_id, **_kwargs: (
                events.append("producer-terminate"),
                setattr(proc, "stopped", True),
            )
        ),
    )

    SimulationRunner._cleanup_done = False
    SimulationRunner._processes[simulation_id] = process
    SimulationRunner._monitor_threads[simulation_id] = Monitor()
    try:
        SimulationRunner.cleanup_all_simulations()
        assert events == ["producer-terminate", "tail-read"]
        assert state.runner_status == RunnerStatus.STOPPED
    finally:
        SimulationRunner._cleanup_done = False
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._monitor_threads.pop(simulation_id, None)
        SimulationRunner._manual_stop_requests.discard(simulation_id)
