import json
import threading
import time
from types import SimpleNamespace

from flask import Flask

from app.api import simulation as simulation_api
from app.config import Config
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import SimulationRunner, SimulationRunState, RunnerStatus
from app.utils.json_files import read_json, write_json_atomic


def _write_prepared_state(sim_dir, *, enable_twitter, enable_reddit):
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "config_generated": True,
                "enable_twitter": enable_twitter,
                "enable_reddit": enable_reddit,
            }
        ),
        encoding="utf-8",
    )
    (sim_dir / "simulation_config.json").write_text("{}", encoding="utf-8")


def test_prepared_check_accepts_single_platform_simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    sim_dir = tmp_path / "sim_twitter_only"
    _write_prepared_state(sim_dir, enable_twitter=True, enable_reddit=False)
    (sim_dir / "twitter_profiles.csv").write_text(
        "user_id,name\n0,Alice\n", encoding="utf-8"
    )

    is_prepared, info = simulation_api._check_simulation_prepared("sim_twitter_only")

    assert is_prepared is True
    assert info["profiles_count"] == 1


def test_prepared_check_still_requires_enabled_platform_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    _write_prepared_state(
        tmp_path / "sim_missing_csv", enable_twitter=True, enable_reddit=False
    )

    is_prepared, info = simulation_api._check_simulation_prepared("sim_missing_csv")

    assert is_prepared is False
    assert "twitter_profiles.csv" in info["missing_files"]


def test_close_env_failure_keeps_previous_state(monkeypatch):
    simulation = SimpleNamespace(status=SimulationStatus.READY)
    saved = []
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "close_simulation_env",
        classmethod(
            lambda _cls, simulation_id, timeout: {
                "success": False,
                "message": "env not running",
            }
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
        "/api/simulation/close-env",
        method="POST",
        json={"simulation_id": "sim-1"},
    ):
        response = simulation_api.close_simulation_env()

    assert response.get_json()["success"] is False
    assert saved == []
    assert simulation.status == SimulationStatus.READY


def _write_actions_log(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_partial_action_line_is_not_consumed(tmp_path):
    complete = json.dumps(
        {
            "round": 1,
            "agent_id": 0,
            "agent_name": "Alice",
            "action_type": "CREATE_POST",
            "action_args": {},
        }
    ) + "\n"
    partial_tail = '{"round": 2, "agent_id": 1, "agent_name": "Bob",'
    log_path = _write_actions_log(
        tmp_path / "twitter" / "actions.jsonl", [complete, partial_tail]
    )
    state = SimulationRunState(
        simulation_id="sim-partial", runner_status=RunnerStatus.RUNNING
    )

    position = SimulationRunner._read_action_log(
        str(log_path), 0, state, "twitter"
    )

    # 文件按平台原生换行写入，因此以文件长度为准计算已消费的字节数
    assert position == log_path.stat().st_size - len(partial_tail.encode("utf-8"))
    assert [action.agent_name for action in state.recent_actions] == ["Alice"]

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write('"action_type": "LIKE_POST", "action_args": {}}\n')

    position = SimulationRunner._read_action_log(
        str(log_path), position, state, "twitter"
    )

    # recent_actions 按最新优先保存
    assert [action.agent_name for action in state.recent_actions] == ["Bob", "Alice"]
    assert position == log_path.stat().st_size


def test_unparsable_complete_line_does_not_rewind_the_offset(tmp_path):
    good = json.dumps(
        {
            "round": 1,
            "agent_id": 0,
            "agent_name": "Alice",
            "action_type": "CREATE_POST",
            "action_args": {},
        }
    ) + "\n"
    bad = "null\n"
    log_path = _write_actions_log(
        tmp_path / "twitter" / "actions.jsonl", [good, bad, good]
    )
    state = SimulationRunState(
        simulation_id="sim-bad-line", runner_status=RunnerStatus.RUNNING
    )

    position = SimulationRunner._read_action_log(
        str(log_path), 0, state, "twitter"
    )
    again = SimulationRunner._read_action_log(
        str(log_path), position, state, "twitter"
    )

    assert position == log_path.stat().st_size
    assert again == position
    assert len(state.recent_actions) == 2


def test_round_start_updates_simulated_hours(tmp_path):
    log_path = _write_actions_log(
        tmp_path / "twitter" / "actions.jsonl",
        [
            json.dumps(
                {"event_type": "round_start", "round": 1, "simulated_hour": 3}
            )
            + "\n",
            json.dumps(
                {"event_type": "round_end", "round": 1, "actions_count": 2}
            )
            + "\n",
        ],
    )
    state = SimulationRunState(
        simulation_id="sim-hours", runner_status=RunnerStatus.RUNNING
    )

    SimulationRunner._read_action_log(str(log_path), 0, state, "twitter")

    assert state.twitter_current_round == 1
    assert state.twitter_simulated_hours == 3
    assert state.simulated_hours == 3


def test_atomic_json_write_is_never_observed_truncated(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"value": -1})
    stop = threading.Event()
    errors = []

    def read_continuously():
        while not stop.is_set():
            try:
                read_json(str(path))
            except Exception as error:  # pragma: no cover - failure path
                errors.append(error)
            # Windows 上 os.replace 需要独占目标文件，读取保持请求级频率
            time.sleep(0.001)

    reader = threading.Thread(target=read_continuously)
    reader.start()
    try:
        for value in range(300):
            write_json_atomic(path, {"value": value, "pad": "x" * 8000})
    finally:
        stop.set()
        reader.join()

    assert errors == []
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["value"] == 299
