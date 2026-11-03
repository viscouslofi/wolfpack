# WolfPack — Pre-Release Plan

Findings from live orchestration testing (2026-11-01), prioritized for a public release.
Target user: someone running this on their own PC with a mix of local models
(Ollama / Bionic / LM Studio), agent CLIs (codex, opencode, kimi, claude), and
free cloud models (OpenRouter & friends). No installs beyond Python 3.11+.

---

## P0 — must fix before release

1. ✅ **Shell quoting for CLI agents (security + breakage).** — DONE: claude uses promptfile redirection, `{prompt}` interpolation now neutralizes quote breakout, kimi moved to API. Verified by tests.
   `kimi`, `opencode`, and `claude` interpolate `{prompt}` into a `shell=True`
   command. On Windows (cmd.exe) the current `\"` escaping does not work — any
   `"` in a task or planner-generated instruction breaks the command, and
   `&`/`|` characters can chain arbitrary commands. codex is immune (stdin).
   Fix: prefer stdin delivery for every CLI that supports it; otherwise build
   argv with `subprocess.list2cmdline` and run with `shell=False`.

2. ✅ **Stop button for runs.** — DONE: /api/stop + cancel events; kills the process tree, marks subtasks/run "stopped", verified live.
   A looping agent (observed: opencode with a weak free model repeated its
   status block for 15 minutes until timeout) can only be stopped by killing
   the server. Need: `/api/stop` → set a cancel flag in status.json, terminate
   the agent process tree, mark the subtask "stopped", and continue to
   synthesis with what exists. UI: a stop button on live turns.

3. **Final artifact must ship in the result.**
   Observed: a README rewrite existed in a subtask result but the synthesis
   only described it. Fixed in prompts (planner must end artifact chains with
   a finalize subtask; synthesis must include the artifact in full) and
   verified live — a poem task ran draft → draft → finalize and the result
   contained the complete final poem.

4. ✅ **Automated test suite.** — DONE: `tests.py`, 11 unit tests (mailbox BOM, inline routing, skills parsing/injection, tiers, review pick, injection immunity, cancel, chain walk).
   Today everything is hand-tested. Add `tests/`: unit tests (mailbox BOM +
   tolerance, inline message routing, stream tee, failover order, chain
   delete guard) and `python harness.py doctor` as the smoke gate. A release
   checklist that runs doctor + one canned 3-agent task end-to-end.

## P1 — quality, should fix

5. ✅ **Capability-aware planning.** — DONE: `tier` in agents.toml + Settings overrides; planner roster shows tiers; review agent is strong-tier only.
   Observed: a local 9B model given a review subtask hallucinated a different
   product and ignored the README. Add a `tier = "strong" | "fast"` field to
   agents.toml, show it in the planner roster, and instruct the planner to
   give review/finalize/synthesis-shaped work to strong agents only.
   (Synthesis already always uses the chosen orchestrator — good.)

6. ✅ **Failover on mid-stream failure.** — DONE: invoke_api_agent catches all exceptions (except user cancel) and fails over.
   `invoke_api_agent` only fails over on `AgentError`; a connection drop
   mid-stream raises something else and skips the remaining models.
   Catch broadly, record, try the next model.

7. ✅ **Continuation should carry the whole thread.** — DONE: walks the chain (max 5 runs / 8k chars), oldest first.
   A follow-up currently receives only its immediate parent's result. Walk
   the `continued_from` chain and feed a compact digest of the full thread
   (cap ~8k chars) so long conversations stay coherent.

8. ✅ **Global mailbox rotation.** — DONE: keeps the last 500 messages.
   `mailbox.jsonl` grows forever. Keep the last ~500 messages on append.

9. ✅ **Scheduler hardening.** — DONE: fired-set capped to today's entries; server-uptime caveat documented in README.
   The `fired` set grows unbounded in a long-running server (cap/clear daily),
   and schedules only run while the server is up — document that clearly in
   the schedules panel and README.

10. **Encoding check on Windows.**
    codex flagged "possible mojibake" reading a UTF-8 README. Verify all file
    reads/writes are explicit `utf-8` (harness side is; spot-check the CLI
    agents' console codepages) and note it in the README for CLI authors.

## P2 — nice to have

11. ✅ **Usage/cost display** — DONE (2026-11-02 wave): per-call token capture (incl. streaming `include_usage`), per-run totals in the result header, per-agent model labels, lifetime totals in Settings, `usage.jsonl` log.
12. **Export a thread** to a single Markdown file (task + result + per-agent
    outputs) for sharing.
13. **Rename runs** in the sidebar (stored in status.json `title` override).
14. **Auto-register new local models** — startup check already detects new
    Ollama/Bionic models; offer a one-click "add as agent" in the mailbox
    message instead of only reporting them.
15. **Version stamp** — `VERSION` file + shown in header, so bug reports are
    meaningful.
16. **MCP outbound** — let API agents call external MCP tool servers (inbound
    `/mcp` endpoint shipped in the 2026-11-02 wave; outbound deferred).
17. **Pinokio / one-click installer packaging** — start.bat / start.sh shipped;
    a Pinokio launcher script would make installs one click for non-devs.

---

## 2026-11-02 feature wave (from competitive research)

All ten ideas from `wolfpack-competitive-research.md` §3, implemented and
verified (18 unit tests + live runs):

1. ✅ **Persistent team memory** — `memory/shared/*.md` injected into planner +
   worker prompts; post-run "learning" phase lets the orchestrator save durable
   facts (`### memory: <slug>` blocks); Settings toggle + folder link.
2. ✅ **Token telemetry** — `usage` captured streaming + non-streaming;
   per-subtask model/usage in status.json, per-run aggregate, lifetime
   `usage.jsonl` + Settings display.
3. ✅ **Council / best-of-n modes** — Mode selector; every selected agent
   answers in parallel, the orchestrator judges (merge vs pick). Verified live.
4. ✅ **Mailbox approval gates** — `type: "approval"` messages pause the run
   (phase *waiting*); the user replies from the mailbox inline box and the run
   resumes with the answer delivered to the team. Unit-tested.
5. ✅ **Inbound MCP** — `POST /mcp` JSON-RPC: initialize / tools/list /
   tools/call with `wolfpack_list_agents`, `wolfpack_run_task`,
   `wolfpack_get_result`. Verified live.
6. ✅ **Docker Sandboxes option** — `docker_sbx_available()` feature-detect +
   per-agent `sandbox = true` + Settings toggle (hidden without Docker).
   Simulated-sbx tests verify the wrap, the host fallback when sbx errors, and
   toggle-off passthrough; a real Windows detection bug (docker.cmd not
   resolving under CreateProcess) was found and fixed. Still untested against
   real Docker — none installed on the dev machine.
7. ✅ **Skill vetting + self-written skills** — download warning + skills start
   disabled + SOURCE.txt provenance; **☆ save as skill** on finished runs
   (agent writes a SKILL.md). Verified live (wrote `list-code-review-benefits`).
8. ✅ **Orchestrator hardening** — planner failover to a second strong agent
   (or a stricter-prompt retry of the same one when the pool has no
   alternative), `extract_json` tolerant of control characters from small local
   models, failed-dependency notes for downstream subtasks, per-batch time caps
   so hung workers can't stall a run, planner roster capped at 12. The failover
   and the JSON tolerance were both exercised by real live failures.
9. ✅ **One-click start** — `start.bat` / `start.sh` check Python ≥ 3.11, start
   the server, open the browser. README quickstart leads with them.
10. ✅ **Delivery routing + quiet mode** — schedules deliver to folder |
    mailbox | webhook; quiet mode teaches the team the `HEARTBEAT_OK` contract
    and skips delivery when nothing matters. Live testing caught small models
    pasting the token onto full reports as a status label — detection now
    requires brevity and the prompt demands the token as the ENTIRE result.
    Both paths verified live on local models (silent run suppressed; reportable
    run delivered to mailbox).
11. ✅ **Project mode** — a fourth orchestration pattern for big builds: the
    lead CLI agent acts as a real developer in a per-run `project/` folder
    (plan → build real files → actually run/test them → fix), looping up to
    three build-test-fix cycles with a strict-JSON loop decision each round,
    while delegating side work to fast cheap models. Build/test steps appear
    as live thought panels, the banner shows the cycle count, and the result
    card links straight to the finished project folder. Verified live: codex
    built and HTMLParser-tested a working clicker game across three cycles;
    the review pass (local Mistral) requested a revision that codex applied,
    and memory captured a real environment lesson (Windows denies Node).
    Stress test (multi-file Python package + pytest suite): codex built
    `csvtool` across 3 cycles — 9/9 tests pass under the user's own Python.
    The loop honestly reported "fail" when pytest was missing from the
    runtime instead of claiming success; the test prompt now tells builders
    to try alternate runners (`py -m pytest`, `python3`, …) before giving up.
12. ✅ **Claude Code + onboarding wizard** — claude CLI integrated (login via
    `/login` in its terminal; wizard opens that terminal for you). First-open
    setup wizard lists every unlinked provider/agent with per-item setup or
    skip; successful tests persist `ready` to agents.toml. Head-to-head on the
    csvtool stress task: codex needed 3 cycles (9 tests, blocked by missing
    pytest); Claude finished in 1 cycle with 50 passing tests in a venv it
    created itself, incl. a 3M-row streaming test — codex's review: pass.
13. ✅ **Deep-review hardening (REVIEW.md)** — all 12 findings fixed:
    cross-platform PATH patching (`os.pathsep`, POSIX dirs), timeout now
    kill-trees the agent process, schedule-name filename sanitization,
    HH:MM validation/normalization (silent dead schedules gone), planner
    output normalization (default fields, deduped ids, unknown-dep pruning,
    real Kahn cycle check), late-stray write guard, 20k per-result caps on
    synthesis/council prompts, capped mailbox rendering, status-aware
    orchestrator dropdowns (composer + settings + schedule form), pattern
    picker for scheduled tasks (incl. project mode), pre-flight orchestrator
    guard, and a budget guardrail (pause-and-ask after N agent calls, 0=off).
    usage.jsonl now logs CLI invocations too, so budgets cover claude/codex.
    Claude set as the default orchestrator. 31/31 tests pass.

## Release checklist (proposed)

- [x] P0 items closed
- [x] `python harness.py doctor` passes on a clean machine profile (tests.py passes: 58/58; doctor exercised live throughout the 2026-11-02 waves)
- [x] One end-to-end canned task run fresh (tips task 2026-11-01; free-model run with telemetry + review + memory 2026-11-02; council-mode run with judge scorecard 2026-11-02)
- [x] README matches reality (features, quickstart, screenshots)
- [x] `keys.toml`, `runs/`, `uploads/`, `mailbox.jsonl` confirmed gitignored (plus settings/schedules/presets/usage/memory and other per-machine state)
- [x] Server binds 127.0.0.1 only by default; documented as a local tool in README §Safety
- [x] install.bat / install.sh rebranded to WolfPack, Claude Code + Ollama hints added, wizard-first flow documented — reviewed line by line; **not** executed on a bare machine (no clean box available on 2026-11-02)
- [x] Version stamp shipped: `VERSION` file, header tag, `WolfPack/<version>` server header

## 1.1.0 packaging (2026-11-02)

- `wolfpack-1.1.0.zip` contains only the app: engine, server, UI, configs,
  examples, installers, README, LICENSE, tests, logo, and the bundled
  `skills/release-notes` skill. No keys, no runs, no per-machine state.
- **Clean-room verified without a second PC**: the zip was extracted to an
  empty sandbox and run with a stripped environment (empty HOME/APPDATA,
  minimal PATH, no key env vars). Result: 58/58 shipped tests pass, server
  starts, wizard fires on first open, CLI agents correctly report
  not-installed, cloud providers needs-key, echo ready for zero-setup trials.
  (Caveat: localhost probes still see this machine's running local servers —
  a true bare machine would show ollama/bionic offline too.)
- The clean-room run caught two real 1.0.0 bugs, both fixed: missing bundled
  skill in the zip, and Unicode prints crashing on stock cp1252 Windows
  consoles (stdout now reconfigured with errors="replace" in all three entry
  points). Paid providers (DeepSeek, Moonshot) are no longer labeled "free"
  in the wizard — `paid = true` flag in providers.toml drives the wording.
