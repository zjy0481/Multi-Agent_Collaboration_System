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
