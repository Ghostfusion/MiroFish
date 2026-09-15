# Working agreement — MiroFish

This file is the standing contract for anyone (human or AI agent) making changes in this
repository. It is short on purpose: read it before your first edit, follow it on every edit.

## 1. Every change is committed **and pushed** — no dangling work

A change is not finished until it is on the remote. Concretely, after you finish and verify
an edit you MUST, in the same task:

```bash
# 1. review what you are about to land
git status --porcelain --untracked-files=all
git diff --cached --stat

# 2. stage the change (explicit paths, never `git add -A`)
git add <the files you changed>

# 3. commit with a Conventional Commit message
git commit -F <message-file>

# 4. push to the protected branch
git push origin main
```

- **Do not** leave a dirty worktree, a stash, or a "commit later" note behind. If you
  changed code, tests, docs, locales or CI in this session, it is committed and pushed in
  this session.
- **Do not** defer docs to a follow-up commit. Docs (`CHANGELOG.md`, `docs/**`, `README*`,
  locale files) are part of the change that alters behaviour, not a separate task.
- Batch commits per coherent change, not per file. Rabbit-hole edits that turn out to be
  unrelated MUST be reverted instead of smuggled into the commit.
- If a change is genuinely unfinished (broken tests, missing prerequisites), say so in the
  commit body and in your report; never push a green-looking commit over a red suite.

## 2. Verify before you push

Run the checks that cover what you touched, and report the real output:

| What you touched | Minimum check |
| --- | --- |
| `backend/app/**`, `backend/tests/**` | `cd backend && .venv/Scripts/python.exe -m pytest -q --no-header -p no:cacheprovider` |
| `backend/scripts/**`, root `tests/**`, `scripts/**` | same command from the repository root |
| `frontend/src/**`, `locales/**` | `cd frontend && npm run build` |
| HTTP contract in `backend/app/api/**` | start `python run.py` and exercise the route (curl/urllib), not only unit tests |
| Docs only | nothing beyond a spell/consistency pass; say so |

Never claim a result you did not observe. A test count, a build result and an endpoint
response in a commit message or report MUST come from a command you actually ran.

## 3. Never commit sensitive or generated material

`.gitignore` already covers these — keep it that way and never force-add them:

- `.env` (real credentials), any `*.pem`/key material
- `backend/uploads/**` (user documents, simulations, reports)
- `backend/logs/**`, `frontend/node_modules/**`, `frontend/dist/**`, `backend/.venv/**`
- `__pycache__`, `.pytest_cache`, OS/IDE droppings

`.env.example` is a **template** and is committed: it documents every supported variable
with safe placeholders and must never contain a real key.

## 4. Commit messages

`type(scope): summary` — `type` ∈ `feat|fix|refactor|perf|docs|test|build|chore`; summary in
the imperative, ≤ 72 characters. The body explains **why**, lists the behaviour that
changed, names the verification you ran, and flags anything left known-broken.

```
fix(report): reject report ids that escape the reports directory

DELETE /api/report/.. resolved to the uploads root and removed every project.

Verification: backend suite 153 passed; live smoke test on :5001.
```

## 5. Repository facts you should not rediscover

| | |
| --- | --- |
| Remote / branch | `origin` = `https://github.com/Ghostfusion/MiroFish.git`, default branch `main` |
| Backend interpreter | `backend/.venv/Scripts/python.exe` (Python 3.12). Never the system interpreter — it has no Flask. |
| Backend entry point | `cd backend && python run.py` (port `5001`, `Flask` + daemon threads) |
| Frontend | `frontend/` — Vue 3 + Vite; `npm install`, `npm run dev` (:3000), `npm run build` |
| Required config | `LLM_API_KEY` plus the graph settings (`GRAPH_BACKEND`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, optional `KUZU_DB_PATH`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`) in `.env` (repo root); startup refuses without them. `.env.example` is the authoritative template. |
| Suites | `backend/tests` (pytest) and the root `tests/` (star-history tooling) |

## 6. Scope discipline

- Fix the source, not the symptom: no suppressing an error, no special-casing an input, no
  shim or alias that keeps a removed path alive.
- Migrate every caller when a contract changes, and delete the code the cutover obsoletes
  (including tests that pin the removed behaviour).
- Tests earn their place only where a plausible bug would fail them. Do not add a test that
  asserts wiring, a copy of a field, or a default.
- Ask before deleting material you did not write; the answer is not "it was unused".

## 7. Reporting

End every task with what changed, the exact verification you ran (counts, exit codes,
endpoint responses), the commit SHA, and anything you deliberately left undone or broken.
