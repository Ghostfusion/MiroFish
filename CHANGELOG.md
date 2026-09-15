# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Simulation activity write-back to Zep removed

The simulation run path no longer writes agent activity into the Zep graph. This is a
scope reduction, requested explicitly: a run is now purely local (actions are counted and
served from the `actions.jsonl` logs), with no Cloud write cost and no ingestion barrier
that can hold a run in `stopping`. The seed ingestion of the graph build (Pipeline 1) is
unchanged — that remains the only graph write the product performs.

- **`POST /api/simulation/start` no longer accepts `enable_graph_memory_update`**
  (`backend/app/api/simulation.py`). The field is gone from the request contract instead of
  being forced to `false`, so no client can ask for the behaviour; the response no longer
  contains `graph_memory_update_enabled` or the conditional `graph_id`, and the endpoint no
  longer takes the per-graph lifecycle lock, re-reads the project graph, or refuses while a
  report is reading the graph.
- **`SimulationRunner` lost the whole enable path** (`backend/app/services/simulation_runner.py`):
  the `enable_graph_memory_update` / `graph_id` parameters, the `_graph_memory_enabled`
  registry, the updater-creation branch, the `stopping` ingestion barrier in the monitor, the
  updater drain on stop / failure / shutdown, and the per-action `graph_updater` hook in
  `_read_action_log()`. `stop_simulation()` now waits on the monitor with a plain 30 s bound
  (`_MONITOR_FINALIZATION_TIMEOUT_SECONDS`) instead of `ZEP_INGESTION_WAIT_TIMEOUT_SECONDS +
  HTTP timeout + 5`, and `cleanup_all_simulations()` enumerates live processes and run states
  only.
- **`ZepGraphMemoryManager` deleted** (`backend/app/services/zep_graph_memory_updater.py`,
  `services/__init__.py`). The per-simulation registry existed only to serve the run path.
  `ZepGraphMemoryUpdater` / `AgentActivity` are retained for the manual Cloud validation
  script (`backend/scripts/validate_zep_cloud_integration.py`) and their unit tests; the
  module docstring now states that it is not wired into the application.
- **Lifecycle guards trimmed to what still exists** (`backend/app/api/graph.py`,
  `backend/app/api/report.py`): graph deletion/reset still refuses while a report reads the
  graph or a simulation run is active (reader lease + `starting/running/paused/stopping` run
  states), but the updater-recovery branch and the `ingestion_pending` response field are
  gone. Report generation still returns `409` while a run is non-terminal — the message now
  says the simulation is active rather than that graph ingestion is.
- **Frontend** (`frontend/src/components/Step3Simulation.vue`,
  `frontend/src/api/simulation.js`): the start payload no longer carries
  `enable_graph_memory_update` and the "dynamic graph memory update enabled" log line is
  removed. `locales/zh.json` / `locales/en.json` dropped `log(s).graphMemoryUpdateEnabled`
  and `api.graphIdRequiredForMemory` (key parity preserved: 645 keys each).
- **Tests**: six obsolete tests deleted (two updater-drain tests in
  `test_zep_simulation_barrier.py`, the updater discard test in
  `test_zep_graph_memory_updater.py`, one in `test_zep_graph_lifecycle.py`, and the two
  ingestion-barrier report tests that the trimmed status guard supersedes), and the tests
  that still describe a real guarantee were rewritten around it: a non-terminal run blocks a
  report, the report reader lease blocks graph deletion, a non-terminal run still blocks a
  project reset, and shutdown terminates the producer before the final tail read. Two tests
  in `test_simulation_state_regressions.py` were added for the monitor's terminal-state write
  (clean exit → `completed`, manual stop → `stopped`), which had no coverage and was briefly
  broken by this refactor.

Verification: backend suite 147 passed (1 pre-existing failure, see below); root suite 173
passed, 2 skipped, same failure; `npm run build` exit 0 (691 modules, only the two
pre-existing warnings); live HTTP smoke test confirmed `POST /api/simulation/start` no longer
validates the removed field and no longer reports it. The one failure,
`test_zep_timeout_policy_is_not_exposed_in_env_example`, is unrelated to this change: the
tracked `.env.example` file has been deleted from the working tree (it is still present in
git), and the test reads it from disk.

Repository-wide defect review of `backend/app`, `backend/scripts`, `frontend/src`,
`scripts/` and the test suites. Twenty-eight defects were fixed; every fix is
covered by the automated suites (153 backend tests, 179 root tests) and a live
HTTP smoke test.

### Security

- **Path traversal via the `platform` query parameter** (`backend/app/api/simulation.py`).
  `/posts`, `/comments`, `/profiles` and `/profiles/realtime` interpolated the raw
  query value into `f"{platform}_simulation.db"`, so `/posts?platform=../../../../tmp/x`
  reached a SQLite file outside the simulation directory. All four routes now
  resolve the platform through `_resolve_platform()` (whitelist `twitter`/`reddit`)
  and answer `400` for anything else.
- **Destructive path traversal via `report_id`** (`backend/app/services/report_agent.py`,
  `backend/app/api/report.py`). `ReportManager` joined a caller-supplied id into the
  reports directory, so `DELETE /api/report/..` resolved to the uploads root and
  `shutil.rmtree` deleted every project, simulation and report. Added
  `ReportManager.is_valid_report_id()` (`^[A-Za-z0-9_-]+$`), enforced at the single
  path choke point (`_get_report_folder`) and at every read/delete entry point;
  invalid ids now return `404` and never touch the file system.
- **Directory creation and traversal via `simulation_id`**
  (`backend/app/services/simulation_manager.py`, `backend/app/api/simulation.py`,
  `backend/app/services/zep_tools.py`). Read paths called `os.makedirs()` for any
  request-supplied id (`mkdir -p` outside the simulations root, and
  `cleanup_simulation_logs()` delete targets). Added `is_valid_simulation_id()`, a
  blueprint-level `before_request` guard, `_get_simulation_dir(create=False)` on all
  read paths, and a guard in the Zep interview profile loader.
- **Internal traceback leakage** (`backend/app/api/*.py`). Every `500` embedded
  `traceback.format_exc()`. Tracebacks are now logged server-side only; the JSON
  error contract stays `{success, error}` (the ontology tests already asserted this).

### Fixed

- **User-triggerable infinite loop in the text chunker**
  (`backend/app/utils/file_parser.py`). `split_text_into_chunks()` could never advance
  when a sentence boundary fell inside the overlap window (`overlap >= 0.3 * chunk_size`),
  hanging the graph-build worker forever. Reachable through
  `POST /api/graph/build` with e.g. `chunk_size=100, chunk_overlap=80`. The boundary is
  now only accepted when it advances past the overlap window and the parameters are
  validated (`0 <= overlap < chunk_size`); output is unchanged for ordinary settings.
- **Unvalidated tool calls aborted whole reports**
  (`backend/app/services/report_agent.py`). The `<tool_call>` branch returned raw JSON,
  so `<tool_call>{}</tool_call>` or the `{"tool": ...}` alias raised `KeyError`/
  `TypeError` and failed the report (or returned `500` from `/api/report/chat`). Parsed
  calls are now validated with `_is_valid_tool_call()`, as the fallback paths already did.
- **Single-platform interviews returned placeholders**
  (`backend/app/services/zep_tools.py`). `interview_agents()` only looked up
  `twitter_{id}`/`reddit_{id}` keys, which single-platform runs never produce (they key
  by bare agent id), so every reply became "（该平台未获得回复）" and a fabricated summary
  was written. Added the bare-key fallback and an explicit failure summary when no
  reply is available at all.
- **Non-atomic state persistence** (`backend/app/utils/json_files.py` (new),
  `models/project.py`, `services/simulation_manager.py`, `services/simulation_runner.py`,
  `services/report_agent.py`, `api/simulation.py`). `open(path, 'w') + json.dump`
  truncated the file before rewriting it, so concurrent readers saw partial JSON and
  raised `JSONDecodeError` (unhandled → `500`). State files are now written with a
  temp file + `os.replace` (`write_json_atomic`) and read with bounded retry on
  Windows sharing errors (`read_json`).
- **`close-env` marked simulations as completed on failure**
  (`backend/app/api/simulation.py`). The route persisted `status = completed` even when
  the close command failed, silently converting a broken run into a finished one.
- **Single-platform simulations were never "prepared"**
  (`backend/app/api/simulation.py`). `_check_simulation_prepared()` required both
  `reddit_profiles.json` and `twitter_profiles.csv`, which a single-platform run never
  writes — preparation re-ran the whole LLM pipeline and `/start` could return
  `400 simNotReady`. Required files now follow `enable_twitter`/`enable_reddit`, and
  `profiles_count` is read from the platform that actually has profiles.
- **Action-log tailing lost and replayed records**
  (`backend/app/services/simulation_runner.py`). A partially written trailing line was
  consumed and dropped; any non-JSON error rewound the read offset, replaying actions
  into the run state and re-sending non-idempotent writes to Zep. The reader now works
  on byte offsets in binary mode, consumes complete lines only, isolates per-line
  failures while still advancing, and the monitor starts at the current end of each log
  so restarting a simulation no longer replays the previous run.
- **`simulated_hours` was always 0** (`backend/app/services/simulation_runner.py`). The
  monitor read `simulated_hours` from `round_end`, which never carries it (the producer
  emits `simulated_hour` on `round_start`), and overwrote the value with 0. Now read
  from `round_start`.
- **Reports for a simulation were resolved arbitrarily**
  (`backend/app/services/report_agent.py`). `get_report_by_simulation()` returned the
  first directory from `os.listdir()`, so chat/status could use a stale report after a
  forced regeneration. It now returns the newest report by `created_at`.
- **Task manager races** (`backend/app/models/task.py`). The singleton published
  `cls._instance` before initialising its lock/task map (concurrent first requests could
  raise `AttributeError`), and `get_task()` handed out the live mutable object. The
  instance is now initialised before publication and `get_task()` returns a consistent
  snapshot.
- **Report download leaked a temp file** (`backend/app/api/report.py`). A
  `NamedTemporaryFile(delete=False)` was never removed and `f.write(None)` raised
  `TypeError` for reports with a null markdown body; the fallback now streams from memory.
- **SQLite connection leak** (`backend/app/api/simulation.py`). `/posts` closed the
  connection after an inner `except sqlite3.OperationalError` block, so any other
  `sqlite3.Error` leaked the handle; both DB routes now use `try/finally`.
- **Ontology fallbacks were dropped** (`backend/app/services/ontology_generator.py`).
  When the model returned more than 10 entity types that already included
  `Person`/`Organization`, the final `[:MAX]` truncation removed exactly those
  mandatory catch-all types (the prompt places them last). Capping now preserves them.
- **Zep activity batches were silently lost** (`backend/app/services/zep_graph_memory_updater.py`).
  `_build_episode_payloads()` ran outside the per-payload `try`, so one bad activity
  destroyed a whole 5-activity batch while `failed_count` stayed 0 and `stop()` reported
  success. Batch build failures are now recorded as failed batches. `action_args: null`
  no longer crashes payload building, and `get_all_stats()` iterates the registry under
  its lock.
- **LLM field types broke interview/search prompts** (`backend/app/services/zep_tools.py`).
  String-valued `questions`/`sub_queries` were iterated per character (numbering every
  character of a sentence); both are now normalized to lists.
- **Negative `max_agents` interviewed almost every agent**
  (`backend/app/services/report_agent.py`). Only the upper bound was clamped, so a
  negative value turned the slice into "everything except the last N"; now `max(1, min(v, 10))`.
- **Malformed requests were reported as server errors** (`backend/app/api/*.py`). A
  non-JSON content type made `request.get_json()` raise `UnsupportedMediaType`, which the
  route's `except Exception` converted into `500` + traceback. All 17 request parsers now
  use `get_json(silent=True)` so the route returns its normal `400` validation error.
- **Initial simulation posts overwrote each other**
  (`backend/scripts/run_twitter_simulation.py`, `backend/scripts/run_parallel_simulation.py`).
  `initial_actions[agent] = ManualAction(...)` silently replaced a second post from the
  same agent while still logging it as published. Both now accumulate a list per agent
  (matching the Reddit path) and report the number of queued posts.
- **Parallel simulations corrupted the log served by the API**
  (`backend/scripts/run_parallel_simulation.py`, `backend/scripts/action_logger.py`). The
  child process opened `<sim_dir>/simulation.log` with mode `w`, truncating and then
  interleaving with the parent's `stdout`/`stderr` capture of the same file.
  `SimulationLogManager` accepts an explicit `main_log_path`; the parallel runner writes
  `<sim_dir>/log/simulation.log`.
- **Stale profile-format checker** (`backend/scripts/test_profile_format.py`). It asserted
  obsolete Twitter/Reddit columns (absent by design) and printed `[错误]` while exiting 0.
  Expectations now match `oasis_profile_generator`, and the script exits non-zero only on
  genuinely missing columns.
- **Frontend polling interval leaked past unmount**
  (`frontend/src/components/Step2EnvSetup.vue`). `startConfigPolling()` overwrote its own
  handle when called from two code paths, so `onUnmounted` could not stop one 2-second
  interval; it now has the same already-running guard as `Step4Report.vue`.
- **d3 tick aborted on hidden labels** (`frontend/src/components/GraphPanel.vue`).
  `getBBox()` was called on `display:none` label nodes (throws in Firefox), killing the
  tick callback every frame while labels were hidden.
- **Duplicate initial loads** (`frontend/src/components/Step5Interaction.vue`). `onMounted`
  and the immediate watchers both fetched the report and profiles; the loads now run once.
- **Unreachable step machine in the graph-build view** (`frontend/src/views/MainView.vue`).
  `currentStep` could only advance through an event the child never emits, and the step-2
  branch mounted `Step2EnvSetup` without its required `simulationId` prop or its
  `@update-status` handler (blank panel, dead wiring). The dead step machine and the
  never-called graph polling were removed; the header shows the graph-build step.
- **Untracked timers and observer** (`frontend/src/components/HistoryDatabase.vue`).
  The mount delay, the nested debounce timers and the lazily created `IntersectionObserver`
  could outlive the component; every handle is now tracked and cleared on unmount.
- **Wrong HTTP method for a report route** (`frontend/src/api/report.js`).
  `getReportStatus()` issued a `GET` against the POST-only `/api/report/generate/status`
  (guaranteed `405`); it now posts a JSON body.
- **Misclassified GitHub rate limit** (`scripts/fetch_star_count.py`). `403` was always
  reported as "request was denied", hiding core rate-limit exhaustion (GitHub does not
  use `429` for it); the `X-RateLimit-Remaining` header is now consulted.
- **`.env` boost placeholders** (`.env`). Shipping non-empty
  `LLM_BOOST_*` placeholders makes the code treat the boost LLM as configured, pointing
  Reddit runs at `your_base_url_here`; the block is commented out.

### Added

- `backend/app/utils/json_files.py` — `write_json_atomic()` / `read_json()` shared
  persistence helpers (temp file + `os.replace`, Windows sharing-violation retry,
  bounded retry on read).
- Regression tests: `backend/tests/test_text_chunker.py`,
  `backend/tests/test_path_guard_regressions.py`,
  `backend/tests/test_report_manager_regressions.py`,
  `backend/tests/test_simulation_state_regressions.py`, plus new cases in
  `backend/tests/test_ontology_generator.py` and
  `backend/tests/test_zep_graph_memory_updater.py`.
- `tests/test_local_star_history.py` — `symlink_or_skip()` helper so the symlink cases
  skip (instead of failing) where symlinks cannot be created without privileges.
- Documentation: `docs/DESIGN.md` (architecture and design), `docs/USER_GUIDE.md`
  (non-technical usage guide).

### Removed

- `frontend/src/views/Process.vue` — an orphaned 2 076-line duplicate of `MainView.vue`
  that nothing imported (the router maps `/process/:projectId` to `MainView.vue`).
- Dead wiring in `MainView.vue`: the local step machine, the `Step2EnvSetup` branch and
  the graph-polling helpers that were never called.

### Verification

| Check | Result |
| --- | --- |
| `backend`: `pytest -q` | 153 passed |
| root: `pytest -q` | 179 passed, 2 skipped (symlink privilege) |
| `cd frontend && npm run build` | exit 0, no new warnings |
| Live server smoke test (`run.py`, :5001) | health 200, platform traversal 400, unsafe simulation id 404, report-delete traversal 404, non-JSON body 400, valid requests 200 |
| `ruff check --select E9,F` | no new findings (remaining items are pre-existing style/dead-code) |

### Known issues (reviewed, intentionally not changed)

- Unbounded id-keyed in-memory maps (`api/graph._build_locks`,
  `utils/zep_lifecycle._graph_locks/_graph_readers`, `SimulationRunner._finalization_locks`,
  `TaskManager` has no scheduled cleanup) grow with the number of projects/graphs seen in
  a process lifetime.
- `ReportConsoleLogger` attaches a `FileHandler` to process-global loggers, so two report
  generations running at once write into each other's `console_log.txt`.
- `SimulationRunner.get_timeline()` / `get_agent_stats()` analyse only the newest 10 000
  actions without signalling that history was dropped.
- `zep_tools.insight_forge()` downgrades per-node read failures to debug logs, which
  contradicts the module's fail-closed policy asserted by
  `backend/tests/test_zep_cloud_contracts.py`.
- Single-platform runs (`platform=twitter|reddit`) produce no `actions.jsonl`, so rounds,
  action counts, `simulated_hours`, `/timeline`, `/agent-stats` and graph-memory updates
  stay empty for them (the web UI always launches `parallel`).
- `simulation_ipc.SimulationIPCClient.send_command()` leaves the pending response file
  behind on timeout; `zep_paging.fetch_all_nodes()` / `fetch_all_edges()` disagree on
  positional parameter order.
