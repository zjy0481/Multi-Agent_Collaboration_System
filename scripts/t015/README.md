# T015 five-assignment live runner

Runs the unchanged official software task 2, newsroom tasks 1 and 2, and fund tasks 1 and 2. Each pair uses the same team and state. Software resumes a copy of the completed software task 1 team. The runner invokes the real course model and may incur cost; it does not initiate a run on import.

From the repository root, use the project's Python environment and configure a real model. For DeepSeek Flash, set `provider = "deepseek"` in the local `llm.toml` (see `llm.example.toml`) and leave the DeepSeek model at its `deepseek-flash` default. This PowerShell example reads the key from the existing `ds_apikey` variable for the current process.

```powershell
$env:COLLABORATION_MODEL_MODE = 'course'
$env:DEEPSEEK_API_KEY = $env:ds_apikey
$env:COURSE_API_TIMEOUT = '120'
$env:COURSE_SEARCH_URL = 'http://127.0.0.1:8888/search'
python scripts/t015/run_five.py --run five-targets-unique-name --software-baseline '<completed T1 scenario directory>' --jobs 3 --stage-seconds 1200
```

Use a fresh run name. Docker must have `collaboration-sandbox:local` available. The baseline must contain `team.json`, `state/runtime.sqlite3`, and the approved task 1 artifact; the script validates its fixed version, SHA-256, approved review, and absence of active or queued goals. A helper starts that accepted app in an isolated, network-disabled container and creates four labeled test bookings through its API. These are **controlled fixture records**, not historical task 1 bookings. The snapshot and its provenance are published into the copied team state for task 2.

Output is written under `.tmp/t015/<run>/`. The `run.json` file records the revision and runtime hashes. Each scenario contains team information, operator events, per-task traces and evaluation reports, active-turn samples, and copied runtime state. Software also records fixture provenance and receipts. The `model-diagnostics.jsonl` file records individual request times, status, and token usage when returned by the provider. The runner logs clarification answers and retries in-scope assignments, task deliveries, and clarification continuations after transient model failures or model-step-limit events. It retries at most twice per delivery, subject to time remaining. An incomplete task is recorded before termination. Tune `--stage-seconds`, `--grace-seconds`, `--idle-seconds`, and `--jobs` as needed.

If the course model pauses (for example after HTTP 402), the runner saves the phase trace, report, team and assignment identifiers and records `provider_paused` in operator events and `runner-result.json`. It stops that scenario without cancelling the assignment or starting the next phase, and exits nonzero. The state directory is retained; this runner does not yet provide cross-process resume, and a raw retained state alone does not guarantee safe resumption. A real deadline with an available provider still follows the existing incomplete-task termination behavior. `runner-result.json` reports runner blocking/errors, not independent task acceptance.

## Fresh software-only run

`--fresh-software` starts a new software team and runs the unchanged official T1 and T2 assignments sequentially, with a fresh state and no seeded history. It is mutually exclusive with `--software-baseline`; the legacy command above retains its five-assignment behavior. Configure a real model using the usual local configuration and environment, then run:

```powershell
python scripts/t015/run_five.py --run fresh-software-unique-name --fresh-software --jobs 1 --stage-seconds 1200
```

The protected assignment guidance requests one developer-to-QA build, a persistent SQLite demonstration, and milestones at 3, 8, 12, 16 and 20 minutes for a 20-minute stage. It requests `t1-app.zip`, `t1-bookings.sqlite3`, and `t1-data-provenance.json`, followed by `t2-dashboard-check.json`. The runner records milestone events and audits the T1 handoff before T2. If both structurally valid versions are readable by developer and QA, their fixed IDs are required inputs for both roles; candidate versions are explicitly identified as candidates when unapproved. Missing data is never synthesized. Audits and trace files are under `.tmp/t015/<run>/software/`; `strict_ready` and the T2 audit are evidence checks, not a substitute for independent application and browser acceptance. `runner-result.json` alone does not establish task success. The team event records the actual loaded model identity. Docker and a configured real provider remain prerequisites; this command makes live model calls.

## Sequential four-task run

`--sequential-four` creates a fresh software team for T1 and T2, then fresh fund and newsroom teams for their T1 only, in that order. Every stage has a 1,200-second deadline with no grace; this mode rejects other stage, grace or concurrency values. It retains software snapshot auditing and applies protected milestone guidance at minutes 3, 8, 12, 16 and 20 to all stages. Each scenario records assignment IDs, milestone and terminal events, trace and reports; `run.json` records the phase plan. A provider pause or runner error stops subsequent scenarios. No historical database is injected.

```powershell
python scripts/t015/run_five.py --run sequential-four-unique-name --sequential-four --jobs 1 --stage-seconds 1200 --grace-seconds 0
```

This is a live model run and requires the configured provider and Docker sandbox image.
