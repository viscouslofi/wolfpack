#!/usr/bin/env python3
"""
Web UI for the multi-agent harness. Stdlib only — no pip installs needed.

    python server.py [--port 7100] [--host 127.0.0.1]

Endpoints:
    GET  /                     the dashboard
    GET  /api/agents           agent registry
    GET  /api/providers        provider info + linked status
    POST /api/keys             {"provider": "...", "key": "..."}  (empty key deletes)
    POST /api/test             {"agent": "..."}  live health check for one agent
    GET  /api/runs             all runs, newest first
    POST /api/run              {"task": "...", "orchestrator": "...", "agents": [...]}
    GET  /api/run/<run_id>     live status + mailbox + result
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import harness

try:
    sys.stdout.reconfigure(errors="replace")  # bare Windows consoles are cp1252
except Exception:
    pass

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
try:
    VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "dev"
UPLOADS = HERE / "uploads"
SCHEDULES_PATH = HERE / "schedules.toml"
GLOBAL_MAILBOX = HERE / "mailbox.jsonl"

TERMINAL_PHASES = ("done", "failed", "stopped", "interrupted")


def recover_interrupted_runs() -> int:
    """Startup recovery pass: any run still showing a live phase when the
    server starts belongs to a dead process — mark it interrupted instead of
    letting it lie in the sidebar as 'working' forever."""
    n = 0
    if not RUNS.exists():
        return 0
    for d in RUNS.iterdir():
        sp = d / "status.json"
        if not sp.exists():
            continue
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if s.get("phase") in TERMINAL_PHASES:
            continue
        s["phase"] = "interrupted"
        s["error"] = ("WolfPack was closed or restarted while this run was "
                      "active — continue it with ↻ if you still need it.")
        for st in (s.get("subtasks") or {}).values():
            if st.get("status") in ("running", "queued"):
                st["status"] = "stopped"
        try:
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            n += 1
        except OSError:
            pass
    if n:
        print(f"[startup] marked {n} interrupted run(s) from a previous session")
    return n


SILENCE_SUSPICIOUS_SEC = 5 * 60
SILENCE_CRITICAL_SEC = 15 * 60


def classify_silence(silence_sec: float) -> str | None:
    """Paperclip-style silent-run signal: a live run with no fresh output for a
    while is suspicious, then critical. Informational only — never auto-kills."""
    if silence_sec >= SILENCE_CRITICAL_SEC:
        return "critical"
    if silence_sec >= SILENCE_SUSPICIOUS_SEC:
        return "suspicious"
    return None


def run_last_activity(d: Path) -> float:
    """Newest mtime among a run's live artifacts (stream logs, mailbox,
    status.json) — the moment the run last said or did anything."""
    latest = 0.0
    candidates = [d / "status.json", d / "mailbox.jsonl"]
    for sub in ("agents", "orchestrator"):
        sd = d / sub
        if sd.is_dir():
            candidates.extend(sd.glob("*.stream.log"))
    for p in candidates:
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    return latest


# ---------------------------------------------------------------- local providers
# Keyless local servers (ollama, bionic, lmstudio): probe reachability and live
# model lists, cached briefly so the UI doesn't hammer them.

_probe_cache = {"ts": 0.0, "data": {}}
_keyed_probe_cache = {"ts": 0.0, "data": {}}


def probe_local_providers() -> dict:
    """Local keyless providers: probed live, cached 15s. Keyed cloud providers:
    probed only when a key exists, cached 10 min (probes run in parallel)."""
    if time.time() - _probe_cache["ts"] >= 15:
        reg = harness.load_registry()
        data = {}
        for pname, pcfg in reg.get("_providers", {}).items():
            if pcfg.get("key_env"):
                continue
            models = harness.probe_provider_models(pcfg)
            data[pname] = {"reachable": models is not None, "models": models or []}
        _probe_cache["ts"] = time.time()
        _probe_cache["data"] = data
    if time.time() - _keyed_probe_cache["ts"] >= 600:
        reg = harness.load_registry()
        targets = [(p, pcfg) for p, pcfg in reg.get("_providers", {}).items()
                   if pcfg.get("key_env") and pcfg.get("models_url")
                   and harness._api_key(p, reg)]
        keyed = {}
        if targets:
            from concurrent.futures import ThreadPoolExecutor
            def probe_one(t):
                pname, pcfg = t
                models = harness.probe_provider_models(
                    pcfg, harness._api_key(pname, reg))
                return pname, {"reachable": models is not None,
                               "models": models or []}
            with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                for pname, result in ex.map(probe_one, targets):
                    keyed[pname] = result
        _keyed_probe_cache["ts"] = time.time()
        _keyed_probe_cache["data"] = keyed
    return {**_probe_cache["data"], **_keyed_probe_cache["data"]}


def ensure_ollama():
    """Start `ollama serve` if the API isn't listening (tray app sometimes isn't)."""
    import shutil as _sh
    import socket
    import subprocess
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return  # already serving
    except OSError:
        pass
    exe = _sh.which("ollama") or str(
        Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe")
    if not Path(exe).exists():
        return
    try:
        subprocess.Popen([exe, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                      | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        print("[server] ollama was not serving — started 'ollama serve'")
    except Exception as e:
        print(f"[server] could not auto-start ollama: {e}")


AVAIL_STATE = HERE / ".harness" / "availability.json"


def snapshot_availability() -> dict:
    """Current availability: local servers + their models, CLI installs, keys."""
    _probe_cache["ts"] = 0  # force fresh probes
    probes = probe_local_providers()
    reg = harness.load_registry()
    return {
        "providers": {p: {"reachable": pr["reachable"], "models": pr["models"]}
                      for p, pr in probes.items()},
        "cli": {n: harness.cli_installed(c)
                for n, c in reg["agents"].items() if c.get("type") == "cli"},
        "keys": {p: bool(harness._api_key(p, reg))
                 for p, pc in reg["_providers"].items() if pc.get("key_env")},
    }


def startup_check():
    """First availability pass, shortly after launch."""
    time.sleep(4)  # give auto-started servers a moment to come up
    availability_check()


def availability_check():
    """Diff availability against the last known state; mailbox the changes.
    Also called periodically from the scheduler loop so newly started local
    servers (or newly added models) show up without a restart."""
    try:
        cur = snapshot_availability()
    except Exception as e:
        print(f"[availability] check failed: {e}")
        return
    old = None
    if AVAIL_STATE.exists():
        try:
            old = json.loads(AVAIL_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    AVAIL_STATE.parent.mkdir(exist_ok=True)
    AVAIL_STATE.write_text(json.dumps(cur, indent=1), encoding="utf-8")

    lines = []
    if old is None:
        up = [p for p, v in cur["providers"].items() if v["reachable"]]
        down = [p for p, v in cur["providers"].items() if not v["reachable"]]
        ready_cli = [n for n, ok in cur["cli"].items() if ok]
        keyed = [p for p, ok in cur["keys"].items() if ok]
        lines.append("First startup check complete.")
        if up:
            lines.append("Local servers running: " + ", ".join(
                f"{p} ({len(cur['providers'][p]['models'])} models)" for p in up))
        if down:
            lines.append("Local servers not running: " + ", ".join(down))
        lines.append("CLI agents installed: " + (", ".join(ready_cli) or "none"))
        lines.append("Cloud keys linked: " + (", ".join(keyed) or "none"))
    else:
        for p, v in cur["providers"].items():
            ov = old.get("providers", {}).get(p)
            if ov is None:
                lines.append(f"New local provider configured: {p}")
                continue
            if v["reachable"] and not ov["reachable"]:
                lines.append(f"🟢 {p} is now running ({len(v['models'])} models)")
            if not v["reachable"] and ov["reachable"]:
                lines.append(f"⚫ {p} is no longer running")
            new = sorted(set(v["models"]) - set(ov["models"]))
            gone = sorted(set(ov["models"]) - set(v["models"]))
            if new:
                lines.append(f"✨ New {p} model(s): " + ", ".join(new))
            if gone:
                lines.append(f"➖ {p} model(s) removed: " + ", ".join(gone))
        for n, ok in cur["cli"].items():
            ook = old.get("cli", {}).get(n)
            if ok and ook is False:
                lines.append(f"🟢 CLI agent now installed: {n}")
            if not ok and ook:
                lines.append(f"⚫ CLI agent no longer found: {n}")
        for p, ok in cur["keys"].items():
            ook = old.get("keys", {}).get(p)
            if ok and not ook:
                lines.append(f"🔑 API key added: {p}")
            if not ok and ook:
                lines.append(f"🔑 API key removed: {p}")
    msg = "\n".join(lines)
    print(f"[availability] {msg}")
    if old is None or (len(lines) and not (len(lines) == 1 and lines[0].startswith("First"))):
        post_global_mailbox("availability check", msg)


def _os_open(target: Path) -> dict:
    import subprocess
    import sys
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606 — local user action
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True, "path": str(target)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_any(path_str: str) -> dict:
    """Open an arbitrary file/folder, but only inside the WolfPack folder."""
    try:
        rp = Path(path_str.strip().rstrip(".,;)")).resolve()
    except Exception:
        return {"ok": False, "error": "bad path"}
    here = HERE.resolve()
    if rp != here and here not in rp.parents:
        return {"ok": False, "error": "path is outside the WolfPack folder"}
    if not rp.exists():
        return {"ok": False, "error": "path does not exist (yet)"}
    return _os_open(rp)


def open_path(run_id: str, what: str) -> dict:
    """Open a run's result file or folder in the OS shell (local machine)."""
    run_dir = (RUNS / Path(run_id).name).resolve()
    if not run_dir.is_dir() or RUNS.resolve() not in run_dir.parents:
        return {"ok": False, "error": "unknown run"}
    if what == "project":
        target = run_dir / "project"
        if not target.is_dir():
            return {"ok": False, "error": "no project folder in this run"}
    else:
        target = run_dir / "result.md" if what == "result" else run_dir
        if what == "result" and not target.exists():
            return {"ok": False, "error": "no result.md yet"}
    return _os_open(target)


def cli_login(agent: str) -> dict:
    """Open a visible terminal window running the agent's CLI so the user can
    sign in interactively (e.g. claude -> /login). Works when WolfPack itself
    was started from a visible terminal (start.bat / start.sh)."""
    reg = harness.load_registry()
    cfg = reg.get("agents", {}).get(agent)
    if not cfg or cfg.get("type") != "cli":
        return {"ok": False, "error": "not a CLI agent"}
    binary = str(cfg.get("command", "")).strip().split(" ", 1)[0].strip('"')
    if not binary or not re.fullmatch(r"[\w.\-]+", binary):
        return {"ok": False, "error": "no runnable CLI command configured"}
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                f'start "WolfPack: sign in to {agent}" cmd /k "{binary}"',
                shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Terminal", binary])
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole"):
                if shutil.which(term):
                    subprocess.Popen([term, "-e", binary])
                    break
            else:
                return {"ok": False,
                        "error": "no terminal emulator found — run "
                                 f"'{binary}' yourself"}
        return {"ok": True, "binary": binary}
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- schedules
# days follow JS convention: 0 = Sunday ... 6 = Saturday (server runs on the
# user's own machine, so local time == the user's timezone).

def load_schedules() -> list[dict]:
    if not SCHEDULES_PATH.exists():
        return []
    import tomllib
    with open(SCHEDULES_PATH, "rb") as f:
        return tomllib.load(f).get("schedules", [])


def save_schedules(schedules: list[dict]) -> None:
    out = []
    for s in schedules:
        days = ", ".join(str(d) for d in s.get("days", []))
        agents = ", ".join(f'"{a}"' for a in s.get("agents", []))
        out.append("[[schedules]]\n" + "\n".join([
            f'id = "{s["id"]}"',
            f'name = "{s["name"].replace(chr(34), "")}"',
            f'task = {json.dumps(s["task"])}',
            f'time = "{s["time"]}"',
            f'days = [{days}]',
            f'agents = [{agents}]',
            f'orchestrator = "{s.get("orchestrator", "")}"',
            f'pattern = "{s.get("pattern", "standard")}"',
            f'output_dir = "{s.get("output_dir", "scheduled-output").replace(chr(92), "/")}"',
            f'notify = {"true" if s.get("notify") else "false"}',
            f'delivery = "{s.get("delivery", "folder")}"',
            f'webhook_url = "{s.get("webhook_url", "").replace(chr(34), "")}"',
            f'quiet = {"true" if s.get("quiet") else "false"}',
            f'catchup = "{s.get("catchup", "skip")}"',
            f'enabled = {"true" if s.get("enabled", True) else "false"}',
            f'last_run = "{s.get("last_run", "")}"',
            f'last_missed_notice = "{s.get("last_missed_notice", "")}"',
        ]))
    SCHEDULES_PATH.write_text("\n\n".join(out) + "\n", encoding="utf-8")


def missed_schedules(schedules: list[dict], now: datetime) -> list[dict]:
    """Enabled schedules whose slot today has already passed without a run —
    i.e. WolfPack was closed at fire time."""
    out = []
    js_day = (now.weekday() + 1) % 7  # Mon=0 -> JS 0=Sun
    today = now.strftime("%Y-%m-%d")
    for s in schedules:
        if not s.get("enabled") or js_day not in s.get("days", []):
            continue
        tm = str(s.get("time") or "")
        if not re.fullmatch(r"\d{2}:\d{2}", tm) or tm >= now.strftime("%H:%M"):
            continue  # no slot today, or the slot is still ahead
        if str(s.get("last_run") or "").startswith(today):
            continue  # already ran today
        out.append(s)
    return out


def post_global_mailbox(frm: str, text: str):
    """Append an app-level message (schedule results, notices) to the global mailbox."""
    try:
        harness.Mailbox(GLOBAL_MAILBOX).append(frm, "user", text)
        # rotation: keep the last 500 messages so the file can't grow forever
        lines = GLOBAL_MAILBOX.read_text(
            encoding="utf-8", errors="replace").splitlines()
        if len(lines) > 500:
            GLOBAL_MAILBOX.write_text("\n".join(lines[-500:]) + "\n",
                                      encoding="utf-8")
    except Exception as e:
        print(f"[mailbox] failed to post: {e}")


def is_heartbeat(text: str) -> bool:
    """Quiet-mode signal: the team decided nothing was worth reporting.
    Must be brief — small models sometimes stick the token on a full report
    as a status label; a long result is a report, not a heartbeat."""
    t = (text or "").strip()
    return "HEARTBEAT_OK" in t and len(t) <= 300


def run_scheduled(sched: dict):
    """Execute one schedule, then deliver the result per its delivery setting:
    folder (default) | mailbox | webhook. Quiet mode skips delivery entirely
    when the run concludes with HEARTBEAT_OK."""
    task = sched["task"]
    if sched.get("quiet"):
        task += ("\n\nThis is a routine scheduled check. If nothing new or "
                 "actionable turned up, the ENTIRE final result must be exactly "
                 "HEARTBEAT_OK — no report, no headings, no explanation. Only "
                 "write a normal report when something IS worth reporting.")
    try:
        out_dir = Path(sched.get("output_dir") or "scheduled-output")
        if not out_dir.is_absolute():
            out_dir = HERE / out_dir
        run_dir = harness.run_task(
            task, sched.get("orchestrator") or None,
            sched.get("agents") or None,
            pattern=sched.get("pattern") or "standard",
            notes_dirs=[str(out_dir)])  # picks up a WOLFPACK.md beside the output
    except Exception as e:
        print(f"[scheduler] '{sched['name']}' failed: {e}")
        if sched.get("notify"):
            post_global_mailbox(
                f"schedule · {sched['name']}",
                f"Scheduled task '{sched['name']}' FAILED: {e}")
        return
    result = run_dir / "result.md"
    if not result.exists():
        return
    text = result.read_text(encoding="utf-8")
    if sched.get("quiet") and is_heartbeat(text):
        print(f"[scheduler] '{sched['name']}' quiet — nothing worth reporting")
        return
    delivery = sched.get("delivery") or "folder"
    dest = None
    if delivery == "mailbox":
        post_global_mailbox(
            f"schedule · {sched['name']}",
            f"Scheduled task '{sched['name']}' finished.\n"
            f"Run details: {run_dir}\n\n{text[:4000]}")
        print(f"[scheduler] '{sched['name']}' -> mailbox")
        return
    if delivery == "webhook" and sched.get("webhook_url"):
        try:
            req = urllib.request.Request(
                sched["webhook_url"],
                data=json.dumps({"name": sched["name"], "run_id": run_dir.name,
                                 "result": text}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "wolfpack/1.0"})
            with urllib.request.urlopen(req, timeout=30):
                pass
            print(f"[scheduler] '{sched['name']}' -> webhook")
        except Exception as e:
            print(f"[scheduler] webhook failed ({e}) — falling back to mailbox")
            post_global_mailbox(
                f"schedule · {sched['name']}",
                f"Webhook delivery failed ({e}). Result:\n\n{text[:3000]}")
        return
    # default: copy result.md into the output folder
    out_dir = Path(sched.get("output_dir") or "scheduled-output")
    if not out_dir.is_absolute():
        out_dir = HERE / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", sched["name"]).strip("-.")[:40]
    dest = out_dir / f"{safe_name or 'run'}-{stamp}.md"
    dest.write_text(text, encoding="utf-8")
    print(f"[scheduler] '{sched['name']}' -> {dest}")
    if sched.get("notify"):
        preview = text[:800] + ("\n…" if len(text) > 800 else "")
        post_global_mailbox(
            f"schedule · {sched['name']}",
            f"Scheduled task '{sched['name']}' finished.\n"
            f"Output saved to: {dest}\nRun details: {run_dir}\n\n{preview}")


def scheduler_loop():
    fired: set[str] = set()
    # catch-up pass: what should have fired while WolfPack was closed?
    try:
        schedules = load_schedules()
        missed = missed_schedules(schedules, datetime.now())
        changed = False
        for s in missed:
            pol = s.get("catchup") or "skip"
            if pol == "run-once":
                print(f"[scheduler] catch-up: firing missed '{s['name']}'")
                s["last_run"] = datetime.now().isoformat(timespec="seconds")
                changed = True
                threading.Thread(target=run_scheduled, args=(s,),
                                 daemon=True).start()
            elif pol == "ask":
                today = datetime.now().strftime("%Y-%m-%d")
                if s.get("last_missed_notice") != today:
                    s["last_missed_notice"] = today
                    changed = True
                    post_global_mailbox(
                        "scheduler",
                        f"Scheduled task '{s['name']}' missed its "
                        f"{s.get('time')} slot while WolfPack was closed. "
                        f"Open 🕒 Scheduled tasks and press ▶ to run it now.")
            else:
                print(f"[scheduler] catch-up: skipping missed '{s['name']}'")
        if changed:
            save_schedules(schedules)
    except Exception as e:
        print(f"[scheduler] catch-up scan failed: {e}")
    last_probe = time.time()  # startup_check already covers t=0
    while True:
        try:
            now = datetime.now()
            if time.time() - last_probe > 600:  # re-probe every ~10 minutes:
                last_probe = time.time()        # local servers come and go
                threading.Thread(target=availability_check,
                                 daemon=True).start()
            if len(fired) > 5000:  # cap memory: drop markers from before today
                fired = {k for k in fired
                         if k.split("@", 1)[1].startswith(f"{now:%Y-%m-%d}")}
            changed = False
            schedules = load_schedules()
            for s in schedules:
                if not s.get("enabled"):
                    continue
                key = f"{s['id']}@{now:%Y-%m-%d %H:%M}"
                js_day = (now.weekday() + 1) % 7  # Mon=0 -> JS 0=Sun
                if (now.strftime("%H:%M") == s.get("time")
                        and js_day in s.get("days", [])
                        and key not in fired):
                    fired.add(key)
                    s["last_run"] = now.isoformat(timespec="seconds")
                    changed = True
                    print(f"[scheduler] firing '{s['name']}'")
                    threading.Thread(target=run_scheduled, args=(s,),
                                     daemon=True).start()
            if changed:
                save_schedules(schedules)
            advance_pipelines()  # chain follow-up steps of finished runs
        except Exception as e:
            print(f"[scheduler] error: {e}")
        time.sleep(30)


def providers_detail() -> dict:
    reg = harness.load_registry()
    probes = probe_local_providers()
    out = {}
    for pname, pcfg in reg.get("_providers", {}).items():
        keyless = not pcfg.get("key_env")
        probe = probes.get(pname, {})
        out[pname] = {
            "signup": pcfg.get("signup", ""),
            "note": pcfg.get("note", ""),
            "key_env": pcfg.get("key_env", ""),
            "keyless": keyless,
            "paid": bool(pcfg.get("paid")),
            "linked": probe.get("reachable", False) if keyless
                      else bool(harness._api_key(pname, reg)),
        }
    return out


def save_key(provider: str, key: str) -> None:
    """Write (or, if empty, remove) a provider key in keys.toml."""
    path = harness.KEYS_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not any(l.strip().startswith("[keys]") for l in lines):
        if lines:
            lines.append("")
        lines.append("[keys]")
    pat = re.compile(rf"^\s*{re.escape(provider)}\s*=")
    for i, l in enumerate(lines):
        if pat.match(l):
            if key:
                lines[i] = f'{provider} = "{key}"'
            else:
                del lines[i]
            break
    else:
        if key:
            idx = next(i for i, l in enumerate(lines)
                       if l.strip().startswith("[keys]"))
            lines.insert(idx + 1, f'{provider} = "{key}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_folder() -> dict:
    """Open the OS-native folder chooser and return the chosen path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        path = filedialog.askdirectory(title="Choose output folder")
        root.destroy()
        return {"path": path or ""}
    except Exception as e:
        return {"path": "", "error": str(e)}


def link_kimi(api_key: str) -> dict:
    """Configure the Kimi Code CLI with a Moonshot API key, non-interactively."""
    import shutil
    import subprocess
    kimi = shutil.which("kimi") or str(
        Path.home() / ".kimi-code" / "bin" / "kimi.exe")
    proc = subprocess.run(
        [kimi, "provider", "catalog", "add", "moonshotai",
         "--api-key", api_key, "--default-model", "kimi-k3"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    ok = proc.returncode == 0
    return {"ok": ok, "output": (proc.stdout + proc.stderr).strip()[:400]}


def list_opencode_models() -> list[str]:
    """Live model list from the opencode CLI (includes free Zen models)."""
    import os
    import subprocess
    env = dict(os.environ)
    extra = [str(Path.home() / ".local" / "bin")]
    if sys.platform == "win32":
        extra.insert(0, os.path.join(os.environ.get("APPDATA", ""), "npm"))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env["XDG_DATA_HOME"] = str((HERE / ".harness" / "xdg-data").resolve())
    try:
        proc = subprocess.run("opencode models", shell=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=30)
        return [l.strip() for l in proc.stdout.splitlines() if "/" in l]
    except Exception:
        return []


def get_settings() -> dict:
    s = harness.load_settings()
    s["sandbox_available"] = harness.docker_sbx_available()
    return s


def post_settings(payload: dict) -> dict:
    cur = harness.load_settings()
    if "orchestrator" in payload:
        cur["orchestrator"] = payload["orchestrator"] or None
    if "max_parallel" in payload:
        cur["max_parallel"] = int(payload["max_parallel"])
    if "review_pass" in payload:
        cur["review_pass"] = bool(payload["review_pass"])
    if "memory_enabled" in payload:
        cur["memory_enabled"] = bool(payload["memory_enabled"])
    if "sandbox_cli" in payload:
        cur["sandbox_cli"] = bool(payload["sandbox_cli"])
    if "onboarding_done" in payload:
        cur["onboarding_done"] = bool(payload["onboarding_done"])
    if "budget_calls" in payload:
        cur["budget_calls"] = max(0, int(payload["budget_calls"] or 0))
    if "skills_enabled" in payload:
        cur["skills_enabled"] = [str(x) for x in payload["skills_enabled"]]
    if "agent_models" in payload:
        cur["agent_models"].update(payload["agent_models"])
        cur["agent_models"] = {k: v for k, v in cur["agent_models"].items() if v}
    if "agent_tiers" in payload:
        cur["agent_tiers"].update(payload["agent_tiers"])
        cur["agent_tiers"] = {k: v for k, v in cur["agent_tiers"].items()
                              if v in ("strong", "fast")}
    harness.save_settings(cur)
    return cur

def usage_totals() -> dict:
    """Lifetime token usage, summed from usage.jsonl."""
    tot = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    p = harness.USAGE_LOG
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tot["input_tokens"] += int(rec.get("input_tokens", 0))
            tot["output_tokens"] += int(rec.get("output_tokens", 0))
            tot["calls"] += 1
    return tot


def agent_scoreboard() -> list[dict]:
    """Per-agent scoreboard: runs, success rate, avg duration (from run
    status.json files) + calls and tokens (from usage.jsonl). Answers
    'which of my agents/models is actually worth using?'"""
    board: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return board.setdefault(name, {
            "agent": name, "subtasks": 0, "done": 0, "failed": 0,
            "elapsed_total": 0.0, "elapsed_n": 0,
            "likes": 0, "dislikes": 0,
            "calls": 0, "input_tokens": 0, "output_tokens": 0})

    if RUNS.exists():
        for d in RUNS.iterdir():
            sp = d / "status.json"
            if not sp.exists():
                continue
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            participants = set()
            if s.get("orchestrator"):
                participants.add(s["orchestrator"])
            for st in (s.get("subtasks") or {}).values():
                name = st.get("agent")
                if not name:
                    continue
                participants.add(name)
                fo = st.get("failover")
                if fo:
                    participants.add(fo)
                if st.get("status") not in ("done", "failed", "stopped"):
                    continue
                if fo:
                    # the original attempt failed; the failover agent's
                    # attempt produced the recorded outcome
                    orow = slot(name)
                    orow["subtasks"] += 1
                    orow["failed"] += 1
                    row = slot(fo)
                else:
                    row = slot(name)
                row["subtasks"] += 1
                row["done" if st["status"] == "done" else "failed"] += 1
                if st.get("elapsed_sec") is not None:
                    row["elapsed_total"] += float(st["elapsed_sec"])
                    row["elapsed_n"] += 1
            rating = s.get("rating") or 0
            for n in participants:
                if rating > 0:
                    slot(n)["likes"] += 1
                elif rating < 0:
                    slot(n)["dislikes"] += 1
    p = harness.USAGE_LOG
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = rec.get("agent")
            if not name:
                continue
            row = slot(name)
            row["calls"] += 1
            row["input_tokens"] += int(rec.get("input_tokens", 0))
            row["output_tokens"] += int(rec.get("output_tokens", 0))
    out = []
    for row in board.values():
        finished = row["done"] + row["failed"]
        row["success_pct"] = round(100 * row["done"] / finished) if finished else None
        row["avg_sec"] = round(row["elapsed_total"] / row["elapsed_n"], 1) \
            if row["elapsed_n"] else None
        del row["elapsed_total"], row["elapsed_n"]
        out.append(row)
    out.sort(key=lambda r: (-r["calls"], r["agent"]))
    return out


# What this work would have cost on a frontier paid API (Sonnet-class),
# per 1M tokens — the yardstick for the free pool's counterfactual value.
REF_RATE_IN, REF_RATE_OUT = 3.0, 15.0
FREE_TIER_PROVIDERS = {"groq", "gemini", "mistral", "cerebras", "github"}


def free_work_value(days: int = 7) -> dict:
    """Dollar value of recent work done by free/local models: tokens from
    keyless local providers, ':free' models and free-tier clouds, priced at
    frontier paid-API rates. CLI subscriptions are flat-rate — excluded."""
    reg = harness.load_registry()
    providers = reg.get("_providers", {})
    agents = reg.get("agents", {})

    def is_free(rec) -> bool:
        cfg = agents.get(rec.get("agent") or "") or {}
        if cfg.get("type") != "api":
            return False
        prov = providers.get(cfg.get("provider", ""), {})
        if prov.get("key_env") == "":
            return True  # local server — keyless
        if ":free" in (rec.get("model") or ""):
            return True
        return cfg.get("provider") in FREE_TIER_PROVIDERS

    cutoff = datetime.now().timestamp() - days * 86400
    tin = tout = 0
    p = harness.USAGE_LOG
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec.get("ts", "")).timestamp()
            except (json.JSONDecodeError, ValueError):
                continue
            if ts < cutoff or not is_free(rec):
                continue
            tin += int(rec.get("input_tokens", 0))
            tout += int(rec.get("output_tokens", 0))
    return {"days": days, "free_input_tokens": tin, "free_output_tokens": tout,
            "usd_equiv": round(tin * REF_RATE_IN / 1e6
                               + tout * REF_RATE_OUT / 1e6, 2)}


SKILLS_DIR = HERE / "skills"


def download_skill(url: str) -> dict:
    """Fetch a SKILL.md from a GitHub blob/raw URL into skills/<name>/."""
    u = url.strip()
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", u)
    if m:
        u = (f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
             f"/{m.group(3)}/{m.group(4)}")
    if not re.match(r"https?://", u):
        return {"ok": False,
                "error": "needs a URL to a SKILL.md file (GitHub blob or raw link)"}
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "wolfpack/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": f"download failed: {e}"}
    name, _ = harness._parse_skill(text, Path(u).parent.name or "skill")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "skill"
    dest = SKILLS_DIR / safe
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return {"ok": True, "name": safe}


# ------------------------------------------------------- export / import pack
# One zip with the whole setup — agents, providers, settings, schedules,
# skills, memory. keys.toml is NEVER included: a pack is meant to be shared.

PACK_FILES = ("agents.toml", "providers.toml", "settings.toml",
              "schedules.toml", "keys.toml.example")
PACK_DIRS = ("skills", "memory")
PACK_NEVER = ("keys.toml", "runs", "uploads", "usage.jsonl", "mailbox.jsonl")


def export_pack() -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PACK_FILES:
            p = HERE / name
            if p.exists():
                z.write(p, name)
        for dname in PACK_DIRS:
            d = HERE / dname
            if d.is_dir():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        z.write(f, f"{dname}/{f.relative_to(d)}")
    return buf.getvalue()


def import_pack(data: bytes) -> dict:
    """Restore a pack zip. Existing files are backed up to *.bak first.
    keys.toml and run data inside a pack are ignored — keys never travel."""
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid zip file"}
    restored, skipped = [], []
    for info in z.infolist():
        name = info.filename.replace("\\", "/").lstrip("/")
        parts = name.split("/")
        if not name or name.endswith("/") or ".." in parts:
            continue
        if name in PACK_NEVER or parts[0] in PACK_NEVER:
            skipped.append(name)
            continue
        if not (name in PACK_FILES or parts[0] in PACK_DIRS):
            skipped.append(name)
            continue
        dest = HERE / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, Path(str(dest) + ".bak"))
        dest.write_bytes(z.read(info))
        restored.append(name)
    return {"ok": True, "restored": restored, "skipped": skipped}


# ------------------------------------------------------- prompt-injection warn
# Dropped-in files become prompt context; a file that talks to the agent
# instead of the user deserves a heads-up (regex tripwire, lite version).

INJECTION_RE = re.compile(
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+.{0,30}instructions"
    r"|you\s+are\s+now\s+(a|an|the)\s"
    r"|new\s+system\s+prompt"
    r"|do\s+anything\s+now"
    r"|\bDAN\s+mode\b", re.I)


def scan_injection(data: bytes) -> str | None:
    """The matched snippet when a text file looks like it carries injected
    instructions; None for clean or binary files."""
    if len(data) > 2 * 1024 * 1024:
        return None
    try:
        text = data.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    m = INJECTION_RE.search(text)
    return m.group(0)[:80] if m else None


def test_agent(name: str) -> dict:
    reg = harness.load_registry()
    cfg = reg.get("agents", {}).get(name)
    if not cfg:
        return {"ok": False, "output": f"unknown agent '{name}'"}
    import time as _t
    t0 = _t.time()
    try:
        out = harness.invoke_agent(name, cfg, "Reply with exactly: OK",
                                   RUNS / "doctor" / name, reg)
        ok = "OK" in out
        if ok and cfg.get("status") != "ready":
            harness.set_agent_status(name, "ready")  # persist the good news
        return {"ok": ok, "output": out[-600:],
                "elapsed_sec": round(_t.time() - t0, 1)}
    except Exception as e:
        return {"ok": False, "output": str(e)[:600],
                "elapsed_sec": round(_t.time() - t0, 1)}


def list_runs() -> list[dict]:
    out = []
    if RUNS.exists():
        for d in sorted(RUNS.iterdir(), reverse=True):
            sp = d / "status.json"
            if not sp.exists():
                continue
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue  # mid-write; skip this poll round
            active = s["phase"] not in TERMINAL_PHASES
            row = {"run_id": s["run_id"], "task": s["task"],
                   "phase": s["phase"], "started": s["started"],
                   "orchestrator": s["orchestrator"],
                   "continued_from": s.get("continued_from"),
                   "active": active}
            if active:  # silent-run watchdog signal for the sidebar
                silence = time.time() - run_last_activity(d)
                level = classify_silence(silence)
                if level:
                    row["silence"] = level
                    row["silence_min"] = int(silence // 60)
            out.append(row)
    return out


def _chain_family(run_id: str) -> tuple[str, list[str]] | None:
    """(root, chronological family ids) for the thread containing run_id."""
    statuses: dict[str, dict] = {}
    if RUNS.exists():
        for d in RUNS.iterdir():
            sp = d / "status.json"
            if not sp.exists():
                continue
            try:
                statuses[d.name] = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    if run_id not in statuses:
        return None

    def root_of(rid: str) -> str:
        seen, cur = set(), rid
        while True:
            p = statuses.get(cur, {}).get("continued_from")
            if not p or p not in statuses or p in seen:
                return cur
            seen.add(p)
            cur = p

    root = root_of(run_id)
    family = [rid for rid in statuses if root_of(rid) == root]
    family.sort(key=lambda r: statuses[r].get("started", ""))
    return root, family


def chain_detail(run_id: str) -> dict | None:
    """The full conversation thread a run belongs to, oldest first."""
    fam = _chain_family(run_id)
    if not fam:
        return None
    root, family = fam
    return {"chain": [run_detail(r) for r in family], "root": root}


def delete_chain(run_id: str) -> dict:
    """Permanently delete a whole conversation thread from disk."""
    fam = _chain_family(run_id)
    if not fam:
        return {"ok": False, "error": "unknown run"}
    root, family = fam
    for rid in family:
        sp = RUNS / rid / "status.json"
        try:
            phase = json.loads(sp.read_text(encoding="utf-8")).get("phase")
        except Exception:
            phase = None
        if phase not in ("done", "failed", "stopped"):
            return {"ok": False,
                    "error": "this conversation has a run in progress — wait for it to finish first"}
    for rid in family:
        shutil.rmtree(RUNS / rid, ignore_errors=True)
    return {"ok": True, "deleted": family}


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def run_detail(run_id: str) -> dict | None:
    d = RUNS / run_id
    sp = d / "status.json"
    if not sp.exists():
        return None
    try:
        s = json.loads(sp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"run_id": run_id, "phase": "loading"}
    mb = d / "mailbox.jsonl"
    s["mailbox"] = []
    if mb.exists():
        for line in mb.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                s["mailbox"].append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rp = d / "result.md"
    s["result"] = rp.read_text(encoding="utf-8") if rp.exists() else None
    pq = d / "pending_question.json"
    s["pending_question"] = None
    if pq.exists():
        try:
            s["pending_question"] = json.loads(pq.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # live per-agent output streams (tee'd while each agent works)
    adir = d / "agents"
    s["streams"] = {sid: _tail(adir / f"{sid}.stream.log")
                    for sid in (s.get("subtasks") or {})
                    if (adir / f"{sid}.stream.log").exists()}
    odir = d / "orchestrator"
    s["orch_streams"] = {"plan": _tail(odir / "plan.stream.log"),
                         "synth": _tail(odir / "synth.stream.log"),
                         "review": _tail(odir / "review.stream.log"),
                         "revise": _tail(odir / "revise.stream.log")}
    return s


def resolve_uploads(ids: list) -> list[str]:
    """Map upload ids to safe absolute paths inside the uploads dir."""
    out = []
    for uid in ids or []:
        p = UPLOADS / Path(str(uid)).name  # basename only — no path escape
        if p.is_file():
            out.append(str(p))
    return out


def start_run(payload: dict) -> str:
    task = (payload.get("task") or "").strip()
    if not task:
        raise ValueError("task is required")
    agents = payload.get("agents") or None
    orch = payload.get("orchestrator") or None
    files = resolve_uploads(payload.get("files"))
    cont = payload.get("continue_from") or None
    goal = (payload.get("goal") or "").strip() or None
    pattern = payload.get("pattern") or "standard"
    if pattern not in ("standard", "council", "best-of-n", "project"):
        pattern = "standard"
    steps = _clean_pipeline_steps(payload.get("pipeline"))
    holder: dict = {}

    def target():
        try:
            holder["run_id"] = harness.run_task(
                task, orch, agents, files, cont, pattern, goal=goal).name
        except Exception as e:  # status.json already records the failure
            print(f"[server] run failed: {e}")

    t = threading.Thread(target=target, daemon=True)
    t.start()
    # run_task creates the run dir synchronously at start; give it a moment
    t.join(timeout=2.0)
    run_id = holder.get("run_id") or newest_run_id()
    if steps:
        try:
            (RUNS / run_id / "pipeline.json").write_text(
                json.dumps({"steps": steps}, indent=1), encoding="utf-8")
        except OSError as e:
            print(f"[server] could not attach pipeline: {e}")
    return run_id


def _clean_pipeline_steps(raw) -> list[dict]:
    """Validate follow-up steps from the UI: short task texts, known patterns,
    at most 5 steps (the cap counts cleaned steps, not raw input)."""
    steps = []
    for s in (raw or []):
        task = str(s.get("task", "")).strip()[:2000]
        if not task:
            continue
        pattern = s.get("pattern") if s.get("pattern") in (
            "standard", "council", "best-of-n", "project") else None
        goal = str(s.get("goal", "")).strip()[:500] or None
        step = {"task": task}
        if pattern:
            step["pattern"] = pattern
        if goal:
            step["goal"] = goal
        steps.append(step)
        if len(steps) >= 5:
            break
    return steps


def advance_pipelines() -> int:
    """Chain follow-up steps: when a run with a pipeline finishes (done only —
    failures never cascade), start the next step as a continuation of it."""
    advanced = 0
    if not RUNS.exists():
        return 0
    for d in RUNS.iterdir():
        pp = d / "pipeline.json"
        if not d.is_dir() or not pp.exists():
            continue
        sp = d / "status.json"
        try:
            phase = json.loads(sp.read_text(encoding="utf-8")).get("phase")
            pipe = json.loads(pp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = pipe.get("steps") or []
        if phase != "done" or not steps:
            continue
        step, rest = steps[0], steps[1:]
        try:  # pop first, so a retry can never double-fire this step
            if rest:
                pp.write_text(json.dumps({"steps": rest}, indent=1),
                              encoding="utf-8")
            else:
                pp.unlink()
        except OSError:
            continue
        payload = {"task": step["task"], "continue_from": d.name}
        if step.get("pattern"):
            payload["pattern"] = step["pattern"]
        if step.get("goal"):
            payload["goal"] = step["goal"]
        if rest:
            payload["pipeline"] = rest
        print(f"[pipeline] '{d.name}' done -> starting next step: "
              f"{step['task'][:60]}")
        try:
            start_run(payload)
            advanced += 1
        except Exception as e:
            print(f"[pipeline] next step failed to start: {e}")
    return advanced


def newest_run_id() -> str:
    runs = list_runs()
    return runs[0]["run_id"] if runs else ""


def mailbox_reply(run_id: str, answer: str) -> dict:
    """Answer a run's pending approval question — unblocks a paused run."""
    run_dir = (RUNS / Path(run_id).name).resolve()
    if not run_dir.is_dir() or RUNS.resolve() not in run_dir.parents:
        return {"ok": False, "error": "unknown run"}
    if not (run_dir / "pending_question.json").exists():
        return {"ok": False, "error": "this run is not waiting for an answer"}
    (run_dir / "approval-response.json").write_text(
        json.dumps({"answer": answer,
                    "ts": datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8")
    return {"ok": True}


def steer_run(run_id: str, text: str) -> dict:
    """Append a user message to a live run's mailbox. Workers embed the
    mailbox in every subtask prompt, so the team picks the note up on the
    next step — mid-run course correction without stopping anything."""
    run_dir = (RUNS / Path(run_id).name).resolve()
    if not run_dir.is_dir() or RUNS.resolve() not in run_dir.parents:
        return {"ok": False, "error": "unknown run"}
    sp = run_dir / "status.json"
    if not sp.exists():
        return {"ok": False, "error": "run has no status"}
    try:
        phase = json.loads(sp.read_text(encoding="utf-8")).get("phase")
    except json.JSONDecodeError:
        phase = None
    if phase in TERMINAL_PHASES:
        return {"ok": False,
                "error": "run is finished — start a follow-up instead"}
    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    msg = {"from": "user", "to": "all", "text": text,
           "ts": datetime.now().isoformat(timespec="seconds")}
    with (run_dir / "mailbox.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return {"ok": True}


def rate_run(run_id: str, rating: int) -> dict:
    """Thumbs up/down on a run — feeds the scoreboard and the auto
    orchestrator's picks. 0 clears the rating."""
    run_dir = (RUNS / Path(run_id).name).resolve()
    if not run_dir.is_dir() or RUNS.resolve() not in run_dir.parents:
        return {"ok": False, "error": "unknown run"}
    sp = run_dir / "status.json"
    if not sp.exists():
        return {"ok": False, "error": "run has no status"}
    if rating not in (1, -1, 0):
        return {"ok": False, "error": "rating must be 1, -1 or 0"}
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "corrupt status file"}
    st["rating"] = rating
    sp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return {"ok": True, "rating": rating}


def disk_usage() -> dict:
    """Total size of the runs/ archive, for the settings hygiene row."""
    total, count = 0, 0
    if RUNS.exists():
        for d in RUNS.iterdir():
            if not d.is_dir():
                continue
            count += 1
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
    return {"runs_bytes": total, "runs_count": count}


def prune_runs(days: int) -> dict:
    """Delete finished runs older than N days. Live runs are never touched."""
    if days < 1:
        return {"ok": False, "error": "days must be at least 1"}
    cutoff = time.time() - days * 86400
    deleted, freed = 0, 0
    if RUNS.exists():
        for d in RUNS.iterdir():
            sp = d / "status.json"
            if not d.is_dir() or not sp.exists():
                continue
            try:
                phase = json.loads(sp.read_text(encoding="utf-8")).get("phase")
                old = sp.stat().st_mtime < cutoff
            except (OSError, json.JSONDecodeError):
                continue
            if phase not in TERMINAL_PHASES or not old:
                continue
            size = 0
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        size += p.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(d, ignore_errors=True)
            deleted += 1
            freed += size
    return {"ok": True, "deleted": deleted, "freed_bytes": freed}


PRESETS_PATH = HERE / "presets.json"


def load_presets() -> list[dict]:
    """Saved task presets — one-click composer fill-ins."""
    if PRESETS_PATH.exists():
        try:
            out = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
            return out if isinstance(out, list) else []
        except json.JSONDecodeError:
            pass
    return []


def save_presets(presets) -> dict:
    clean = []
    for p in (presets or []):  # cap applies to cleaned presets, not raw input
        task = str(p.get("task", "")).strip()[:500]
        if not task:
            continue
        pattern = p.get("pattern") if p.get("pattern") in (
            "standard", "council", "best-of-n", "project") else "standard"
        clean.append({
            "name": str(p.get("name", "")).strip()[:40] or task[:40],
            "task": task, "pattern": pattern,
            "orchestrator": str(p.get("orchestrator", ""))[:40],
            "agents": [str(a)[:40] for a in (p.get("agents") or [])][:12],
        })
        if len(clean) >= 12:
            break
    PRESETS_PATH.write_text(json.dumps(clean, indent=1, ensure_ascii=False),
                            encoding="utf-8")
    return {"ok": True, "presets": clean}


def save_run_as_skill(run_id: str) -> dict:
    """Turn a finished run into a reusable SKILL.md (the team writes it)."""
    run_dir = RUNS / Path(run_id).name
    sp = run_dir / "status.json"
    rp = run_dir / "result.md"
    if not sp.exists() or not rp.exists():
        return {"ok": False, "error": "run has no finished result to learn from"}
    try:
        task = json.loads(sp.read_text(encoding="utf-8")).get("task", "")
    except json.JSONDecodeError:
        task = ""
    result = rp.read_text(encoding="utf-8", errors="replace")
    reg = harness.load_registry()
    settings = harness.load_settings()
    agents = reg["agents"]
    orch = settings.get("orchestrator") or reg.get("orchestrator", "codex")
    pick = harness.pick_review_agent(orch, agents, settings)  # strong, ready
    name, cfg = pick if pick else (orch, agents.get(orch))
    if not cfg:
        return {"ok": False, "error": "no ready agent available to write the skill"}
    try:
        out = harness.invoke_agent(
            name, cfg,
            harness.SKILL_WRITER_PROMPT.format(task=task[:4000],
                                               result=result[:10000]),
            RUNS / "skill-writer" / name)
    except Exception as e:
        return {"ok": False, "error": f"skill writer failed: {e}"}
    slug = harness.save_skill_from_text(
        out, f"written by {name} from run {run_dir.name}")
    if not slug:
        return {"ok": False, "error": "the agent did not produce a SKILL.md"}
    return {"ok": True, "name": slug}


# ---------------------------------------------------------------- MCP (inbound)
# Minimal Model Context Protocol server over HTTP POST /mcp (JSON-RPC 2.0) so
# other agent tools can drive WolfPack: list agents, start a task, poll results.

MCP_TOOLS = [
    {"name": "wolfpack_list_agents",
     "description": "List WolfPack's configured agents and whether each is ready.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wolfpack_run_task",
     "description": "Start a WolfPack task: the orchestrator plans, the agent "
                    "team executes, and a final result is synthesized. Returns "
                    "a run_id immediately; poll wolfpack_get_result for output.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "task": {"type": "string"},
                         "orchestrator": {"type": "string"},
                         "agents": {"type": "array",
                                    "items": {"type": "string"}},
                         "pattern": {"type": "string",
                                     "enum": ["standard", "council", "best-of-n",
                                              "project"]}},
                     "required": ["task"]}},
    {"name": "wolfpack_get_result",
     "description": "Get a run's phase and (when finished) its final result.",
     "inputSchema": {"type": "object",
                     "properties": {"run_id": {"type": "string"}},
                     "required": ["run_id"]}},
]


def _mcp_result(req_id, text: str):
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]}}


def _mcp_error(req_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def mcp_dispatch(req: dict):
    """One JSON-RPC call. Returns None for notifications (no response body)."""
    req_id = req.get("id")
    method = req.get("method", "")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"protocolVersion": "2025-06-18",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "wolfpack", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name == "wolfpack_list_agents":
            reg = harness.load_registry()
            lines = [f"{n}: {c.get('status', '?')} — {c.get('description', '')}"
                     for n, c in reg["agents"].items()]
            return _mcp_result(req_id, "\n".join(lines))
        if name == "wolfpack_run_task":
            task = str(args.get("task", "")).strip()
            if not task:
                return _mcp_error(req_id, -32602, "task is required")
            run_id = start_run({"task": task,
                                "orchestrator": args.get("orchestrator"),
                                "agents": args.get("agents"),
                                "pattern": args.get("pattern")})
            return _mcp_result(
                req_id,
                f"run started: {run_id}\nPoll with wolfpack_get_result.")
        if name == "wolfpack_get_result":
            d = run_detail(str(args.get("run_id", "")))
            if not d:
                return _mcp_error(req_id, -32602, "unknown run_id")
            out = f"phase: {d.get('phase')}\n"
            if d.get("error"):
                out += f"error: {d['error']}\n"
            if d.get("result"):
                out += f"\n{d['result']}"
            return _mcp_result(req_id, out)
        return _mcp_error(req_id, -32601, f"unknown tool '{name}'")
    return _mcp_error(req_id, -32601, f"unknown method '{method}'")


class Handler(BaseHTTPRequestHandler):
    server_version = f"WolfPack/{VERSION}"

    def log_message(self, fmt, *args):  # keep console quiet
        pass

    def _body(self) -> dict:
        """Read a JSON request body, tolerating non-UTF-8 bytes from odd clients."""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        return json.loads(raw.decode("utf-8", "replace"))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            return self._file(HERE / "index.html", "text/html; charset=utf-8")
        if path == "/logo.png":
            lp = HERE / "logo.png"
            if not lp.exists():
                self.send_response(404)
                self.end_headers()
                return
            return self._file(lp, "image/png")
        if path == "/api/agents":
            reg = harness.load_registry()
            settings = harness.load_settings()
            probes = probe_local_providers()
            agents = {}
            for n, c in reg["agents"].items():
                status = c.get("status", "?")
                models = list(c.get("models", []))
                prov = c.get("provider", "")
                if c.get("type") == "api" and prov in probes:
                    # keyless local provider: health = server reachability
                    pr = probes[prov]
                    status = "ready" if pr["reachable"] else "offline"
                    if pr["models"]:  # offer live models, configured ones first
                        models = list(dict.fromkeys(models + pr["models"]))
                agents[n] = {"description": c.get("description", ""),
                             "strengths": c.get("strengths", []),
                             "type": c.get("type", "cli"),
                             "provider": prov,
                             "tier": harness.agent_tier(n, c, settings),
                             "models": models,
                             "install": c.get("install", ""),
                             "current_model": settings["agent_models"].get(n)
                                               or c.get("default_model")
                                               or (models or [""])[0],
                             "status": status}
            return self._json({"orchestrator": settings.get("orchestrator")
                               or reg.get("orchestrator"),
                               "agents": agents})
        if path == "/api/providers":
            return self._json({"providers": providers_detail()})
        if path == "/api/settings":
            s = get_settings()
            s["version"] = VERSION
            return self._json(s)
        if path == "/api/models/opencode":
            return self._json({"models": list_opencode_models()})
        if path == "/api/usage":
            return self._json(usage_totals())
        if path == "/api/scoreboard":
            return self._json({"scoreboard": agent_scoreboard(),
                               "free_week": free_work_value()})
        if path == "/api/disk":
            return self._json(disk_usage())
        if path == "/api/presets":
            return self._json({"presets": load_presets()})
        if path == "/api/export":
            body = export_pack()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             'attachment; filename="wolfpack-pack.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/skills":
            en = set(get_settings().get("skills_enabled", []))
            sks = harness.scan_skills()
            for sk in sks:
                sk["enabled"] = sk["id"] in en
            return self._json({"skills": sks})
        if path == "/api/schedules":
            return self._json({"schedules": load_schedules(),
                               "timezone": datetime.now().astimezone().tzname()})
        if path == "/api/runs":
            return self._json({"runs": list_runs()})
        if path == "/api/mailbox":
            msgs = []
            if GLOBAL_MAILBOX.exists():
                for line in GLOBAL_MAILBOX.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    try:
                        msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return self._json({"messages": msgs})
        if path.startswith("/api/run/"):
            detail = run_detail(path.rsplit("/", 1)[-1])
            return self._json(detail, 200) if detail else self._json(
                {"error": "unknown run"}, 404)
        if path.startswith("/api/chain/"):
            ch = chain_detail(path.rsplit("/", 1)[-1])
            return self._json(ch, 200) if ch else self._json(
                {"error": "unknown run"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/run":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                run_id = start_run(payload)
                return self._json({"run_id": run_id})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 30 * 1024 * 1024:
                    return self._json({"error": "file too large (25 MB max)"}, 413)
                p = json.loads(self.rfile.read(length) or b"{}")
                name = re.sub(r"[^A-Za-z0-9._-]+", "_",
                              Path(p.get("name", "file")).name)[:80] or "file"
                data = base64.b64decode(p.get("data_b64", ""))
                if len(data) > 25 * 1024 * 1024:
                    return self._json({"error": "file too large (25 MB max)"}, 413)
                UPLOADS.mkdir(exist_ok=True)
                uid = f"{int(time.time() * 1000)}-{name}"
                (UPLOADS / uid).write_bytes(data)
                resp = {"id": uid, "name": name, "size": len(data)}
                hit = scan_injection(data)
                if hit:
                    resp["warning"] = (
                        f"⚠ this file contains text that looks like injected "
                        f"instructions (“{hit}”). Agents will read it as context "
                        f"— make sure you trust it.")
                return self._json(resp)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/keys":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                save_key(payload["provider"], (payload.get("key") or "").strip())
                _keyed_probe_cache["ts"] = 0  # re-probe with the new key
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/stop":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                rid = str(payload.get("run_id", ""))
                ev = harness.RUN_CANCEL.get(rid)
                if ev:
                    ev.set()
                    return self._json({"ok": True})
                return self._json({"ok": False,
                                   "error": "run not active (already finished?)"})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/test":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                return self._json(test_agent(payload.get("agent", "")))
            except Exception as e:
                return self._json({"ok": False, "output": str(e)}, 200)
        if path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                return self._json({"ok": True, "settings": post_settings(payload)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/skills":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                if p.get("url"):
                    return self._json(download_skill(str(p["url"])))
                if "enabled" in p:
                    return self._json({"ok": True, "settings":
                                       post_settings({"skills_enabled": p["enabled"]})})
                return self._json({"error": "url or enabled required"}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/kimi-link":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                key = (p.get("api_key") or "").strip()
                if not key:
                    return self._json({"ok": False, "output": "api_key required"})
                return self._json(link_kimi(key))
            except Exception as e:
                return self._json({"ok": False, "output": str(e)})
        if path == "/api/pick-folder":
            return self._json(pick_folder())
        if path == "/api/import":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 60 * 1024 * 1024:
                    return self._json({"error": "pack too large"}, 413)
                p = json.loads(self.rfile.read(length) or b"{}")
                data = base64.b64decode(p.get("data_b64", ""))
                return self._json(import_pack(data))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/cli-login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                return self._json(cli_login(p.get("agent", "")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if path == "/api/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                return self._json(delete_chain(str(p.get("run_id", ""))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                if p.get("what") == "memory":
                    harness.MEMORY_DIR.mkdir(exist_ok=True)
                    return self._json(_os_open(harness.MEMORY_DIR))
                if p.get("path"):
                    return self._json(open_any(str(p["path"])))
                return self._json(open_path(p.get("run_id", ""),
                                            p.get("what", "folder")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/mailbox/reply":
            try:
                p = self._body()
                return self._json(mailbox_reply(str(p.get("run_id", "")),
                                                str(p.get("answer", ""))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/steer":
            try:
                p = self._body()
                return self._json(steer_run(str(p.get("run_id", "")),
                                            str(p.get("text", ""))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/rate":
            try:
                p = self._body()
                return self._json(rate_run(str(p.get("run_id", "")),
                                           int(p.get("rating", 0))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/prune":
            try:
                p = self._body()
                return self._json(prune_runs(int(p.get("days", 30))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/presets":
            try:
                p = self._body()
                return self._json(save_presets(p.get("presets")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/save-skill":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                return self._json(save_run_as_skill(str(p.get("run_id", ""))))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/mcp":
            try:
                req = self._body()
            except json.JSONDecodeError:
                return self._json(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}, 400)
            resp = mcp_dispatch(req)
            if resp is None:  # JSON-RPC notification — acknowledge, no body
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(resp)
        if path == "/api/schedule":
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = json.loads(self.rfile.read(length) or b"{}")
                schedules = load_schedules()
                action = p.get("action")
                if action == "create":
                    if not p.get("task") or not p.get("time") or not p.get("days"):
                        return self._json({"error": "task, time and days are required"}, 400)
                    tm = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(p["time"]))
                    if not tm or not (0 <= int(tm.group(1)) <= 23
                                      and 0 <= int(tm.group(2)) <= 59):
                        return self._json({"error": "time must be HH:MM (24h)"}, 400)
                    norm_time = f"{int(tm.group(1)):02d}:{int(tm.group(2)):02d}"
                    pattern = p.get("pattern") or "standard"
                    if pattern not in ("standard", "council", "best-of-n", "project"):
                        return self._json({"error": "unknown pattern"}, 400)
                    schedules.append({
                        "id": f"s{int(time.time()*1000)}",
                        "name": (p.get("name") or p["task"][:40]).strip(),
                        "task": p["task"], "time": norm_time,
                        "days": [int(d) for d in p["days"] if 0 <= int(d) <= 6],
                        "agents": p.get("agents", []),
                        "orchestrator": p.get("orchestrator", ""),
                        "pattern": pattern,
                        "output_dir": p.get("output_dir", "scheduled-output"),
                        "notify": bool(p.get("notify")),
                        "delivery": p.get("delivery")
                                    if p.get("delivery") in ("folder", "mailbox", "webhook")
                                    else "folder",
                        "webhook_url": str(p.get("webhook_url") or "").strip(),
                        "quiet": bool(p.get("quiet")),
                        "catchup": p.get("catchup")
                                   if p.get("catchup") in ("skip", "run-once", "ask")
                                   else "skip",
                        "enabled": True, "last_run": "",
                    })
                    save_schedules(schedules)
                    return self._json({"ok": True, "schedules": schedules})
                target = next((s for s in schedules if s["id"] == p.get("id")), None)
                if not target:
                    return self._json({"error": "unknown schedule"}, 404)
                if action == "delete":
                    schedules.remove(target)
                    save_schedules(schedules)
                elif action == "toggle":
                    target["enabled"] = not target.get("enabled", True)
                    save_schedules(schedules)
                elif action == "run":
                    threading.Thread(target=run_scheduled, args=(target,),
                                     daemon=True).start()
                else:
                    return self._json({"error": "unknown action"}, 400)
                return self._json({"ok": True, "schedules": schedules})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7100)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    recover_interrupted_runs()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=ensure_ollama, daemon=True).start()
    threading.Thread(target=startup_check, daemon=True).start()
    print(f"Agent Harness UI -> http://{args.host}:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
