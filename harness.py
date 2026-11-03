#!/usr/bin/env python3
"""
Multi-agent harness: one orchestrator agent decomposes a task, delegates
subtasks to CLI agents (codex / kimi / opencode / ...), agents exchange
messages through a shared mailbox, and the orchestrator synthesizes the
final result.

Usage:
    python harness.py run "Build a REST API for a todo app with tests"
    python harness.py run "..." --orchestrator codex --agents codex,kimi
    python harness.py agents           # list agents and their status
    python harness.py doctor           # headless health check per agent
    python harness.py show <run_id>    # print a past run's final result

Layout per run (under harness/runs/<run_id>/):
    plan.json            the orchestrator's task decomposition
    mailbox.jsonl        inter-agent messages (merged from outboxes)
    agents/<subtask_id>/ isolated workdir per subtask (incl. outbox.jsonl)
    subtask results in results.json
    result.md            final synthesized report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")  # bare Windows consoles are cp1252
except Exception:
    pass

HARNESS_DIR = Path(__file__).resolve().parent
RUNS_DIR = HARNESS_DIR / "runs"
REGISTRY_PATH = HARNESS_DIR / "agents.toml"
PROVIDERS_PATH = HARNESS_DIR / "providers.toml"
KEYS_PATH = HARNESS_DIR / "keys.toml"
SETTINGS_PATH = HARNESS_DIR / "settings.toml"
MEMORY_DIR = HARNESS_DIR / "memory"
USAGE_LOG = HARNESS_DIR / "usage.jsonl"


def load_settings() -> dict:
    """User preferences: default orchestrator, per-agent model picks, parallelism."""
    s = _load_toml(SETTINGS_PATH) if SETTINGS_PATH.exists() else {}
    return {
        "orchestrator": s.get("orchestrator"),
        "max_parallel": int(s.get("max_parallel", 5)),
        "agent_models": s.get("agent_models", {}),
        "review_pass": bool(s.get("review_pass", True)),
        "skills_enabled": list(s.get("skills_enabled", [])),
        "agent_tiers": s.get("agent_tiers", {}),
        "memory_enabled": bool(s.get("memory_enabled", True)),
        "sandbox_cli": bool(s.get("sandbox_cli", False)),
        "onboarding_done": bool(s.get("onboarding_done", False)),
        "budget_calls": int(s.get("budget_calls", 0)),
        "agent_call_cap": int(s.get("agent_call_cap", 0)),
        "verify_pass": bool(s.get("verify_pass", True)),
    }


def agent_tier(name: str, cfg: dict, settings: dict | None = None) -> str:
    """strong | fast — settings override beats agents.toml; default: cli=strong, api=fast."""
    t = ((settings or {}).get("agent_tiers") or {}).get(name) or cfg.get("tier")
    if t in ("strong", "fast"):
        return t
    return "strong" if cfg.get("type") == "cli" else "fast"


def save_settings(settings: dict) -> None:
    lines = []
    if settings.get("orchestrator"):
        lines.append(f'orchestrator = "{settings["orchestrator"]}"')
    lines.append(f'max_parallel = {int(settings.get("max_parallel", 5))}')
    lines.append(f'review_pass = {"true" if settings.get("review_pass", True) else "false"}')
    lines.append(f'memory_enabled = {"true" if settings.get("memory_enabled", True) else "false"}')
    if settings.get("sandbox_cli"):
        lines.append("sandbox_cli = true")
    if settings.get("onboarding_done"):
        lines.append("onboarding_done = true")
    if settings.get("budget_calls"):
        lines.append(f'budget_calls = {int(settings["budget_calls"])}')
    if settings.get("agent_call_cap"):
        lines.append(f'agent_call_cap = {int(settings["agent_call_cap"])}')
    if not settings.get("verify_pass", True):
        lines.append("verify_pass = false")
    if settings.get("skills_enabled"):
        arr = ", ".join(json.dumps(str(x)) for x in settings["skills_enabled"])
        lines.append(f"skills_enabled = [{arr}]")
    if settings.get("agent_models"):
        lines.append("")
        lines.append("[agent_models]")
        for agent, model in settings["agent_models"].items():
            lines.append(f'"{agent}" = "{model}"')
    if settings.get("agent_tiers"):
        lines.append("")
        lines.append("[agent_tiers]")
        for agent, tier in settings["agent_tiers"].items():
            if tier in ("strong", "fast"):
                lines.append(f'"{agent}" = "{tier}"')
    SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- registry

def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_registry() -> dict:
    """Agents config with providers and keys resolved in."""
    reg = _load_toml(REGISTRY_PATH)
    reg["_providers"] = _load_toml(PROVIDERS_PATH)
    keys = _load_toml(KEYS_PATH).get("keys", {})
    reg["_keys"] = keys
    for name, cfg in reg.get("agents", {}).items():
        if cfg.get("type") == "cli" and not cli_installed(cfg):
            cfg["status"] = "not-installed"
            continue
        if cfg.get("type") != "api":
            continue
        prov = reg["_providers"].get(cfg.get("provider", ""), {})
        if not prov.get("key_env"):
            # keyless local provider (ollama, bionic, lmstudio) — never needs a key
            if cfg.get("status") == "needs-key":
                cfg["status"] = "ready"
        elif not _api_key(cfg["provider"], reg):
            cfg["status"] = "needs-key"
        elif cfg.get("status") == "needs-key":
            cfg["status"] = "ready"
    return reg


def set_agent_status(name: str, status: str) -> None:
    """Persist an agent's status inside its [agents.<name>] block in agents.toml
    (e.g. needs-login -> ready after a successful login + probe)."""
    text = REGISTRY_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_block = False
    status_line = None
    block_start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("["):
            if in_block:
                break
            in_block = s == f"[agents.{name}]"
            if in_block:
                block_start = i
            continue
        if in_block and s.startswith("status"):
            status_line = i
    if block_start is None:
        return
    if status_line is not None:
        lines[status_line] = f'status = "{status}"'
    else:
        lines.insert(block_start + 1, f'status = "{status}"')
    REGISTRY_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _api_key(provider: str, reg: dict) -> str:
    key = reg.get("_keys", {}).get(provider) or ""
    if not key:
        env = reg.get("_providers", {}).get(provider, {}).get("key_env", "")
        key = os.environ.get(env, "") if env else ""
    return key.strip()


# ---------------------------------------------------------------- adapters

class AgentError(Exception):
    pass


class AgentCancelled(Exception):
    """Raised when the user stops a run mid-flight."""


# run_id -> cancel Event; the server's /api/stop endpoint sets these
RUN_CANCEL: dict[str, threading.Event] = {}

# per-thread record of the last API call's token usage + model (thread-local so
# parallel subtasks don't clobber each other)
_USAGE = threading.local()


def _usage_of(data: dict) -> dict | None:
    """Normalize an OpenAI-style usage object to {input_tokens, output_tokens}."""
    u = data.get("usage")
    if not isinstance(u, dict):
        return None
    it = u.get("prompt_tokens") or u.get("input_tokens") or 0
    ot = u.get("completion_tokens") or u.get("output_tokens") or 0
    if not (it or ot):
        return None
    return {"input_tokens": int(it), "output_tokens": int(ot)}


def take_call_meta() -> dict | None:
    """Pop metadata ({agent, model, usage}) recorded by the last API call on
    this thread. None for CLI agents (they report no token counts)."""
    meta = getattr(_USAGE, "call", None)
    _USAGE.call = None
    return meta


def record_usage(status: "RunStatus", where: str, meta: dict | None,
                 agent: str | None = None) -> None:
    """Aggregate token usage into the run's status.json + append usage.jsonl.
    `calls` counts every agent invocation (CLI agents report no tokens but
    still count — subscription rate limits apply to them too). `agent` is the
    real agent name; `where` (the step id) is only a fallback."""
    agg = status.state.setdefault("usage",
                                  {"input_tokens": 0, "output_tokens": 0, "calls": 0})
    agg["calls"] += 1
    agent_name = (meta or {}).get("agent") or agent or where
    per = status.state.setdefault("usage_by_agent", {})
    pa = per.setdefault(agent_name, {"calls": 0, "input_tokens": 0,
                                     "output_tokens": 0})
    pa["calls"] += 1
    if meta and meta.get("usage"):
        u = meta["usage"]
        agg["input_tokens"] += u.get("input_tokens", 0)
        agg["output_tokens"] += u.get("output_tokens", 0)
        pa["input_tokens"] += u.get("input_tokens", 0)
        pa["output_tokens"] += u.get("output_tokens", 0)
    status.save()
    u = (meta or {}).get("usage") or {"input_tokens": 0, "output_tokens": 0}
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                "run_id": status.state["run_id"], "where": where,
                                "agent": agent_name,
                                "model": (meta or {}).get("model"),
                                **u}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_stream(resp, stream_file: Path, cancel_event=None) -> str:
    """Read an SSE chat stream, teeing every token into stream_file as it lands."""
    parts: list[str] = []
    reasoning: list[str] = []
    usage = None
    with open(stream_file, "a", encoding="utf-8") as f:
        for raw in resp:
            if cancel_event is not None and cancel_event.is_set():
                raise AgentCancelled("stopped by user")
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            u = _usage_of(chunk)
            if u:
                usage = u
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            think = delta.get("reasoning") or delta.get("reasoning_content") or ""
            piece = delta.get("content") or ""
            if think:
                reasoning.append(think)
                f.write(think)
                f.flush()
            if piece:
                parts.append(piece)
                f.write(piece)
                f.flush()
    _USAGE.usage = usage
    out = "".join(parts).strip()
    if not out and reasoning:  # reasoning-only models: thoughts are the answer
        out = "".join(reasoning).strip()
    return out


def api_chat(base_url: str, key: str, model: str, prompt: str,
             timeout: int = 180, stream_file: Path | None = None,
             cancel_event=None) -> str:
    """One OpenAI-compatible chat completion via stdlib urllib.
    With stream_file set, tokens are appended to that file as they arrive."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
    }
    if stream_file is not None:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "agent-harness/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if stream_file is not None and e.code in (400, 404, 405, 422):
            # this server rejected streaming — retry as a plain request
            return api_chat(base_url, key, model, prompt, timeout, None,
                            cancel_event)
        raise AgentError(f"HTTP {e.code} from {base_url}: {detail}") from e
    except urllib.error.URLError as e:
        raise AgentError(f"cannot reach {base_url}: {e.reason}") from e
    with resp:
        if stream_file is not None:
            return _read_stream(resp, stream_file, cancel_event)
        try:
            data = json.loads(resp.read().decode("utf-8"))
            _USAGE.usage = _usage_of(data)
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise AgentError(f"unexpected response shape: {str(data)[:300]}") from e


def invoke_api_agent(name: str, cfg: dict, prompt: str, reg: dict,
                     stream_file: Path | None = None,
                     cancel_event=None) -> str:
    """Try each configured model in order until one answers."""
    provider = cfg["provider"]
    pcfg = reg["_providers"].get(provider)
    if not pcfg:
        raise AgentError(f"unknown provider '{provider}'")
    key = _api_key(provider, reg)
    if not key and pcfg.get("key_env"):
        raise AgentError(f"no API key for {provider} — add it to keys.toml")
    models = list(cfg.get("models", []))
    # a user-picked model (settings.toml) jumps to the front of the failover chain
    pick = load_settings()["agent_models"].get(name)
    if pick and pick in models:
        models.remove(pick)
        models.insert(0, pick)
    elif pick:
        models.insert(0, pick)
    if not models:
        # dynamic agent: use whatever the (local) server is currently serving
        live = probe_provider_models(pcfg, key)
        if not live:
            raise AgentError(
                f"{provider} has no models available — is the app running "
                f"and is a model loaded?")
        models = live
    errors = []
    for model in models:
        try:
            if stream_file is not None:
                stream_file.parent.mkdir(parents=True, exist_ok=True)
                with open(stream_file, "a", encoding="utf-8") as f:
                    f.write(f"--- {name} via {provider}/{model} ---\n")
            out = api_chat(pcfg["base_url"], key or "none", model, prompt,
                           cfg.get("timeout_sec", 180), stream_file,
                           cancel_event)
            _USAGE.call = {"agent": name, "model": f"{provider}/{model}",
                           "usage": getattr(_USAGE, "usage", None)}
            _USAGE.usage = None
            return f"[{name} via {provider}/{model}]\n\n{out}"
        except AgentCancelled:
            raise  # a user stop is not a model failure — no failover
        except Exception as e:  # any failure (incl. mid-stream) → next model
            errors.append(f"{model}: {e}")
            if stream_file is not None:
                try:
                    with open(stream_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[{model} failed: {e} — trying next model]\n")
                except OSError:
                    pass
            time.sleep(1)  # be polite to rate limiters
    raise AgentError(f"all models failed for {name}:\n" + "\n".join(errors))


NON_CHAT_HINTS = ("embed", "image", "tts", "whisper", "rerank")


def probe_provider_models(pcfg: dict, key: str = "") -> list[str] | None:
    """Live model list from a provider's models_url.
    Returns None when the server is unreachable, [] when reachable but empty."""
    url = pcfg.get("models_url")
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key or 'none'}",
                          "User-Agent": "agent-harness/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m["id"] for m in data.get("data", [])
                if m.get("id")
                and not any(h in m["id"].lower() for h in NON_CHAT_HINTS)]
    except Exception:
        return None


def cli_installed(cfg: dict) -> bool:
    """Is this CLI agent's binary on the machine?"""
    m = re.match(r'\s*"?([A-Za-z0-9_.-]+)"?', cfg.get("command", ""))
    if not m:
        return True
    exe = m.group(1)
    if shutil.which(exe):
        return True
    extra = [os.path.join(os.environ.get("APPDATA", ""), "npm"),
             str(Path.home() / ".local" / "bin"),
             str(Path.home() / ".kimi-code" / "bin")]
    for d in extra:
        if not d:
            continue
        for cand in (exe, exe + ".exe", exe + ".cmd"):
            if (Path(d) / cand).exists():
                return True
    return False


def _kill_tree(proc):
    """Kill a process and, on Windows, its whole tree (cmd.exe spawns children)."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def invoke_agent(name: str, cfg: dict, prompt: str, workdir: Path,
                 reg: dict | None = None,
                 stream_file: Path | None = None,
                 cancel_event=None,
                 resume: bool = False) -> str:
    """Run one agent headlessly in `workdir`, return its text output.
    With stream_file set, the agent's output is teed there as it is produced.
    With resume=True and a `resume_command` in agents.toml, the CLI continues
    its previous session in `workdir` instead of starting a fresh one —
    used for follow-up runs so the orchestrator keeps its working context."""
    _USAGE.call = None  # stale meta from an earlier call on this thread
    _USAGE.usage = None
    if cfg.get("type") == "echo" or name == "echo":
        return f"[echo agent received prompt of {len(prompt)} chars]\n\n{prompt[:500]}"
    if cfg.get("type") == "api":
        workdir.mkdir(parents=True, exist_ok=True)
        return invoke_api_agent(name, cfg, prompt, reg or load_registry(),
                                stream_file, cancel_event)

    workdir.mkdir(parents=True, exist_ok=True)
    template = cfg["command"]
    if resume and cfg.get("resume_command"):
        template = cfg["resume_command"]
        print(f"[harness] {name}: resuming previous CLI session")
    # per-agent model override from settings, else the agent's default
    model = load_settings()["agent_models"].get(name) or cfg.get("default_model", "")
    template = template.replace("{model}", model)
    env = dict(os.environ)
    for k, v in (cfg.get("env") or {}).items():
        env[k] = str((HARNESS_DIR / v).resolve()) if not os.path.isabs(v) else v
    # npm-installed CLIs live here; uv tools here:
    extra_dirs = [str(Path.home() / ".local" / "bin"),
                  str(Path.home() / ".kimi-code" / "bin")]
    if os.name == "nt":
        extra_dirs.insert(0, os.path.join(os.environ.get("APPDATA", ""), "npm"))
    env["PATH"] = os.pathsep.join(extra_dirs + [env.get("PATH", "")])

    if cfg.get("promptfile"):
        # Prompt goes to a file, fed to the CLI via shell redirection —
        # fully immune to shell quoting/injection (", &, |, % all safe).
        pf = workdir / ".prompt.txt"
        pf.write_text(prompt, encoding="utf-8")
        cmd = (template.replace("{workdir}", str(workdir))
                       .replace("{promptfile}", str(pf)))
        stdin_data = None
    elif cfg.get("stdin"):
        cmd = template.replace("{workdir}", str(workdir))
        stdin_data = prompt
    else:
        # Pass the prompt as a single double-quoted arg. cmd.exe has no escape
        # for " inside a quoted string, so replace it with ' — with no " left,
        # nothing in the prompt can close the quote and break out to the shell.
        safe_prompt = prompt.replace('"', "'")
        cmd = template.replace("{workdir}", str(workdir)).replace("{prompt}", safe_prompt)
        stdin_data = None

    if stream_file is not None:
        stream_file.parent.mkdir(parents=True, exist_ok=True)
        with open(stream_file, "a", encoding="utf-8") as f:
            f.write(f"--- {name} (cli) ---\n")

    # experimental: Docker Sandboxes (docker sbx) — the agent's workdir is
    # mounted into a microVM so the CLI can't touch the rest of the host
    sandboxed = False
    host_cmd = cmd
    if (cfg.get("sandbox") and load_settings().get("sandbox_cli")
            and docker_sbx_available()):
        sandboxed = True
        cmd = f'docker sbx run --rm -v "{workdir}:/work" -w /work {cmd}'

    def run_once(command: str) -> tuple[str, str, int]:
        proc = subprocess.Popen(
            command,
            shell=cfg.get("shell", True),
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(workdir),
            env=env,
        )
        out_parts: list[str] = []
        err_parts: list[str] = []
        tee = (open(stream_file, "a", encoding="utf-8")
               if stream_file is not None else None)

        def pump(src, dst, tee_file):
            try:
                for line in src:
                    dst.append(line)
                    if tee_file:
                        tee_file.write(line)
                        tee_file.flush()
            except (ValueError, OSError):
                pass

        t_out = threading.Thread(target=pump, args=(proc.stdout, out_parts, tee),
                                 daemon=True)
        t_err = threading.Thread(target=pump, args=(proc.stderr, err_parts, None),
                                 daemon=True)
        t_out.start()
        t_err.start()
        try:
            if stdin_data is not None:
                try:
                    proc.stdin.write(stdin_data)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            deadline = time.time() + cfg.get("timeout_sec", 900)
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    _kill_tree(proc)
                    t_out.join(2)
                    t_err.join(2)
                    if tee:
                        tee.close()
                    raise AgentCancelled(f"{name} stopped by user")
                if time.time() > deadline:
                    raise subprocess.TimeoutExpired(command,
                                                    cfg.get("timeout_sec", 900))
                time.sleep(0.2)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)  # not just the shell — the agent process too
            t_out.join(5)
            t_err.join(5)
            if tee:
                tee.close()
            raise AgentError(f"{name} timed out after {cfg.get('timeout_sec', 900)}s")
        t_out.join()
        t_err.join()
        if tee:
            tee.close()
        return "".join(out_parts).strip(), "".join(err_parts).strip(), proc.returncode

    out, err, rc = run_once(cmd)
    if sandboxed and rc != 0 and re.search(
            r"not a docker command|unknown command|error during connect|"
            r"cannot connect to the docker|no such (image|container)", err, re.I):
        print(f"[harness] docker sbx failed for {name} — retrying on the host")
        out, err, rc = run_once(host_cmd)
    if rc != 0 and not out:
        raise AgentError(f"{name} exited {rc}: {err[:800]}")
    return out + (f"\n\n[stderr]\n{err[:800]}" if err and rc != 0 else "")


# ---------------------------------------------------------------- mailbox

class Mailbox:
    """JSONL message bus. Agents write outbox.jsonl in their workdir;
    the harness merges those into mailbox.jsonl after each subtask."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.touch(exist_ok=True)

    def append(self, frm: str, to: str, text: str, mtype: str | None = None):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "from": frm, "to": to, "text": text}
        if mtype:
            rec["type"] = mtype
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def all(self) -> list[dict]:
        with self._lock:
            out = []
            for l in self.path.read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                try:
                    out.append(json.loads(l))
                except json.JSONDecodeError:
                    continue  # tolerate partial lines or agents writing here directly
            return out

    def render(self, max_msgs: int = 12, max_chars: int = 3000) -> str:
        """Render recent messages for a prompt. Capped: the mailbox can grow
        without bound during a run, and every subtask prompt embeds it."""
        msgs = self.all()
        if not msgs:
            return "(mailbox is empty)"
        msgs = msgs[-max_msgs:]
        lines, total = [], 0
        for m in reversed(msgs):  # newest first, then flip back
            line = f"- [{m['from']} -> {m['to']}]: {m['text']}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(reversed(lines)) or "(mailbox is empty)"

    def merge_outbox(self, subtask_id: str, workdir: Path):
        outbox = workdir / "outbox.jsonl"
        if not outbox.exists():
            return
        # utf-8-sig: some agents write their outbox with a BOM
        for line in outbox.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                m = json.loads(line)
                self.append(subtask_id, str(m.get("to", "all")),
                            str(m.get("text", "")),
                            str(m["type"]) if m.get("type") else None)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------- prompts

PLAN_PROMPT = """You are the orchestrator of a multi-agent harness. Decompose the task below into subtasks for specialist agents.

Available agents:
{agent_roster}

Enabled skills the team can use: {skills}

{memory_block}Rules:
- Assign each subtask to the best-suited agent by strengths.
- Use "depends_on" for ordering; independent subtasks run in parallel.
- A subtask receives the RESULT text of every subtask it depends on, plus the files
  those subtasks produced (under prior_work/<subtask_id>/ for agents with file access,
  inlined into the prompt for agents without).
- Teamwork is the point: spread subtasks across DIFFERENT agents. Never give
  everything to one agent (including yourself) unless the task is trivial.
- Mind the tier: review, finalize, and polish subtasks go to strong-tier agents
  only; fast-tier agents get drafts, brainstorms, and simple steps.
- For open-ended, design, or opinion tasks, brainstorm: give 2-3 agents the same
  question in parallel with different angles, then synthesize the best ideas.
- For code or writing tasks, add a review subtask assigned to a different agent
  than the author — the reviewer gets the work handed off automatically.
- When the task produces a final artifact (a document, code file, README, plan),
  end the chain with a finalize subtask (depends_on the review) that applies the
  review feedback and outputs the complete final artifact text.
- Keep subtasks self-contained: each agent only sees its own instructions plus a shared mailbox.
- 2-5 subtasks is usually right. Do not over-split.

Respond with STRICT JSON only, no prose, no code fences:
{{
  "subtasks": [
    {{"id": "t1", "title": "short title", "agent": "<agent name>",
      "instructions": "detailed instructions for the agent",
      "depends_on": []}}
  ],
  "synthesis_guidance": "how to combine the subtask results into the final answer"
}}

TASK:
{task}
"""

SUBTASK_PROMPT = """You are agent "{agent}" working as part of a multi-agent team on a larger task.
Your subtask: {title}

Instructions:
{instructions}

Shared team mailbox (messages from other agents and the orchestrator):
{mailbox}

{prior_results}
{memory_block}{context_files}{skills_block}{file_access}
When finished, end your reply with a clear section "RESULT:" summarizing concrete
outputs (files created, findings, code).
"""

FILE_ACCESS_CLI = """If you need to leave a message for another agent, the orchestrator, or the user
(questions for the user are surfaced in the UI), append JSON lines
to a file named outbox.jsonl in your current working directory, one per line:
{{"to": "<agent|orchestrator|user>", "text": "your message"}}
If you are BLOCKED and cannot continue without an answer from the user, add
"type": "approval" to the message — the whole run pauses until the user replies
in the mailbox, and their answer is delivered back to the team:
{{"to": "user", "type": "approval", "text": "Which format do you want: X or Y?"}}
Only use this when truly blocked — never for optional clarification.

Do your work in the current working directory.
"""

FILE_ACCESS_API = """You have NO file access — deliver every concrete output (code, documents,
reviews) directly in your reply text. To leave a message for another agent, the
orchestrator, or the user, put one JSON object per line in your reply:
{{"to": "<agent|orchestrator|user>", "text": "your message"}}
The harness routes those lines to the team mailbox automatically.
If you are BLOCKED and cannot continue without an answer from the user, add
"type": "approval" to the message — the run pauses until the user replies.
Only use this when truly blocked — never for optional clarification.
"""

SYNTH_PROMPT = """You are the orchestrator. The original task was:

{task}

Synthesis guidance: {guidance}

Subtask results:
{results}

Final mailbox state:
{mailbox}

Write the final consolidated result in Markdown: what was done, key outputs per subtask,
file paths produced, and any open issues or follow-ups. Be concrete and complete.
If the task produced a final artifact (code, README, document), include the COMPLETE
final artifact text in your answer — never just describe it or point at it.
Do NOT redo the subtasks' work yourself — only consolidate what the agents produced.
"""

REVIEW_PROMPT = """You are the final reviewer for a multi-agent run. The original task was:

{task}

The team's final result:
{result}

Critique the result against the task: correctness, completeness, anything fabricated
or missing. Be brief and specific. If it is good as-is, say so.
End your reply with exactly one line: VERDICT: PASS  or  VERDICT: REVISE
"""

REVISE_PROMPT = """You are the orchestrator. The original task was:

{task}

The team's final result:
{result}

The reviewer's critique:
{critique}

Apply the critique and output the improved final result in Markdown. If the critique
is wrong on a point, keep the original and say why. Include any final artifact in full.
"""

MEMORY_PROMPT = """You are the memory keeper for a multi-agent team. A run just finished.

Original task:
{task}

Final result (excerpt):
{result}

Decide what is worth remembering ACROSS FUTURE RUNS — durable facts only:
user preferences, project conventions, environment quirks, useful file locations,
things that failed and why. Do NOT restate the task results themselves.

If nothing is worth remembering, reply with exactly: NONE
Otherwise output one or more blocks in exactly this format:
### memory: <topic-slug>
<concise markdown notes, 2-8 lines>
"""

COUNCIL_MEMBER_NOTE = ("Answer completely and independently — other agents are "
                       "answering the same task in parallel and a judge will "
                       "compare all answers afterwards.")

COUNCIL_PROMPT = """You are the judge of a council of AI agents who each answered the same task independently.

Original task:
{task}

The council's answers:
{answers}

Compare the answers: where they agree, where they conflict, and what each got
uniquely right. Then deliver the single best consolidated answer to the original
task in Markdown — merge the strongest parts, drop what was wrong or weak, and
note any disagreement that remains unresolved. Include any final artifact
(code, document) in full.
"""

BESTOF_PROMPT = """You are the judge of a competition: several AI agents each produced a draft answer to the same task.

Original task:
{task}

The drafts:
{answers}

Pick the single best draft. Output it in full (lightly edited to fix small flaws
if needed), then end with one short line: "Chosen: <draft id> — <one-sentence reason>".
"""

SCORE_NOTE = """Finally, score each answer 1-10 for correctness, completeness and clarity.
End your reply with exactly one line, no prose after it:
SCORES: {"c1": 8, "c2": 6}
(use the draft ids as keys.)
"""

_SCORES_RE = re.compile(r"\n?\s*SCORES:\s*(\{[^{}]*\})\s*$")


def _parse_scores(text: str) -> tuple[str, dict]:
    """Strip the judge's trailing SCORES line; return (clean text, scores)."""
    m = _SCORES_RE.search(text)
    if not m:
        return text, {}
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return text, {}
    scores = {str(k): max(1, min(10, int(round(v))))
              for k, v in raw.items() if isinstance(v, (int, float))}
    return text[: m.start()].rstrip() + "\n", scores

PROJECT_PLAN_PROMPT = """You are the lead developer of a team building a real project on this machine.

Project goal:
{task}

Team you can delegate to (cheap agents for grunt work — research, boilerplate,
copy text, asset lists, small isolated functions). They have no context beyond
what you write in their instructions:
{agent_roster}

{memory_block}Rules:
- YOU are the builder: you create and edit the real files in your working
  directory, wire them together, and run them.
- Delegate only well-scoped side work; never delegate the core architecture.
- The project must fit {max_cycles} build-test-fix cycles — scope accordingly.

Respond with STRICT JSON only, no prose, no code fences:
{{
  "architecture": "3-6 sentences: what gets built, file layout, how it runs",
  "delegate": [{{"id": "d1", "title": "short", "agent": "<name>",
                "instructions": "self-contained instructions"}}],
  "first_steps": "what you build first"
}}
"""

PROJECT_BUILD_PROMPT = """You are the lead developer building a real project in your current
working directory. Do the work NOW: create and edit real files, keep everything
runnable. You may run shell commands to check your work.

Project goal:
{task}

Architecture plan:
{architecture}

{state_block}{prior_results}{mailbox_block}
End your reply with a section "RESULT:" listing the files you created or
changed and what currently works.
"""

PROJECT_TEST_PROMPT = """You are the lead developer. TEST what you built in the current working
directory: run the test suite, compile, execute the entry point, or at minimum
syntax-check every file — whatever proves it works. Actually run things; do not
just read the code. If the usual tool is missing (pytest, node, …), try the
alternatives a developer would: `py -m pytest`, `python3`, other interpreters
or runtimes on PATH — before concluding it cannot be run.

The goal was:
{task}

Reply with STRICT JSON only, no prose, no code fences:
{{"tests_run": "commands you ran and their outcomes",
  "bugs": ["every bug, gap, or missing piece vs the goal — empty list if none"],
  "status": "pass" or "fail"}}
"""

PROJECT_LOOP_PROMPT = """You are the lead developer deciding whether a project is finished.

Goal:
{task}

Cycle {cycle} of {max_cycles} just finished.
Test outcome: {test_report}

Your build notes from this cycle:
{build_notes}

Rules:
- "done" only when the goal is genuinely met AND tests pass.
- Otherwise "continue" with a concrete next focus; you may delegate side work.
- The final cycle must end "done" — use it to polish, not to start new features.

Reply with STRICT JSON only, no prose, no code fences:
{{"status": "done" or "continue",
  "summary": "one-paragraph state of the project",
  "delegate": [{{"id": "d{cycle}x", "title": "short", "agent": "<name>",
                "instructions": "self-contained instructions"}}],
  "next_focus": "what you build or fix next cycle"}}
"""

PROJECT_SYNTH_PROMPT = """You are the lead developer writing the final report for a finished project.

Goal:
{task}

Architecture:
{architecture}

What happened across the build-test-fix cycles:
{cycle_log}

The project lives in the folder: {project_dir}

Write the final report in Markdown: what was built, the file tree, how to run
or open it, what was tested, and known limitations. Be concrete.
"""

VERIFY_PROMPT = """You are the verifier for a finished project. Another agent claims the
work is done — your job is to check that claim against the evidence, not to
redo the work.

The goal was:
{task}

The final report claims:
{report}

Evidence from disk (actual files and test logs):
{evidence}

Check specifically:
- Do the files the report claims exist actually exist, and are they non-trivial
  (not empty stubs)?
- Did tests actually run and pass, or was "done" declared without proof?
- Any obvious gap between the goal and what is on disk?

Reply with STRICT JSON only, no prose, no code fences:
{{"verdict": "pass" or "concerns",
  "notes": "2-4 sentences: what you checked and what (if anything) is missing"}}
"""

SKILL_WRITER_PROMPT = """You convert a successful multi-agent run into a reusable SKILL.md —
an instruction pack that lets any agent repeat this kind of work next time.

The task the team solved:
{task}

The final result:
{result}

Write a concise SKILL.md. Start with YAML frontmatter:
---
name: lowercase-hyphen-slug
description: one line — when an agent should reach for this skill
---
Then short sections: when to use it, what inputs are needed, the step-by-step
procedure that worked, and pitfalls to avoid. Keep it under 60 lines.
Output ONLY the SKILL.md content, starting with the --- frontmatter.
"""


def _agent_provider(name: str, cfg: dict) -> str:
    """What rate-limit pool an agent draws from: its API provider, or itself
    for CLI agents (each subscription is its own pool)."""
    return cfg.get("provider") or (name if cfg.get("type") == "cli" else "")


def pick_failover_agent(name: str, agents: dict,
                        settings: dict | None = None) -> str | None:
    """A different ready agent of the same tier — retry a failed subtask once
    instead of letting one rate limit sink the step. Prefers a DIFFERENT
    provider: account-wide rate limits sink every model on the same provider
    together, so same-provider failover is usually pointless."""
    tier = agent_tier(name, agents.get(name) or {}, settings)
    prov = _agent_provider(name, agents.get(name) or {})
    ready = [(n, c) for n, c in agents.items()
             if n != name and c.get("status") in ("ready", "degraded")
             and c.get("type") != "echo"]
    cands = [n for n, c in ready if agent_tier(n, c, settings) == tier]
    if not cands:  # no same-tier peer — any ready agent beats a dead step
        cands = [n for n, _ in ready]
    cands.sort(key=lambda n: 1 if prov and _agent_provider(n, agents[n]) == prov
               else 0)
    return cands[0] if cands else None


def pick_review_agent(orch: str, agents: dict,
                      settings: dict | None = None) -> tuple[str, dict] | None:
    """Choose a reviewer that isn't the orchestrator — strong tier first,
    then by strengths, preferring a different provider so account-wide rate
    limits don't take out the primary and the fallback together."""
    prefer = ("review", "reasoning", "analysis", "general", "writing")
    orch_prov = _agent_provider(orch, agents.get(orch) or {})
    cands = [(n, c) for n, c in agents.items()
             if n != orch and n != "echo" and c.get("type") != "echo"
             and c.get("status") == "ready"]

    def score(kv):
        n, c = kv
        st = c.get("strengths", [])
        rank = next((i for i, t in enumerate(prefer) if t in st), len(prefer))
        tier_rank = 0 if agent_tier(n, c, settings) == "strong" else 1
        same_prov = 1 if orch_prov and _agent_provider(n, c) == orch_prov else 0
        # provider diversity first: this picker backs up the orchestrator at
        # synthesis/judge time, when its provider may be rate-limited as a whole
        return (same_prov, tier_rank, rank)

    pool = list(cands)
    pool.sort(key=score)
    return pool[0] if pool else None


def pick_auto_orchestrator(agents: dict,
                           settings: dict | None = None) -> str | None:
    """Choose the orchestrator from past performance: subtask success rate
    first, then user run ratings, then speed. With no history every candidate
    ties, which falls back to the first ready strong-tier agent."""
    cands = [n for n, c in agents.items()
             if c.get("status") in ("ready", "degraded")
             and c.get("type") != "echo"
             and agent_tier(n, c, settings) == "strong"]
    if not cands:
        return None
    stats = {n: {"done": 0, "failed": 0, "elapsed": 0.0, "n": 0,
                 "likes": 0, "dislikes": 0} for n in cands}
    if RUNS_DIR.is_dir():
        for rd in RUNS_DIR.iterdir():
            p = rd / "status.json"
            if not p.is_file():
                continue
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            participants = set()
            orch = st.get("orchestrator")
            if orch in stats:
                participants.add(orch)
            for sub in (st.get("subtasks") or {}).values():
                a = sub.get("failover") or sub.get("agent")
                if a not in stats:
                    continue
                participants.add(a)
                if sub.get("status") == "done":
                    stats[a]["done"] += 1
                elif sub.get("status") == "failed":
                    stats[a]["failed"] += 1
                el = sub.get("elapsed_sec")
                if isinstance(el, (int, float)):
                    stats[a]["elapsed"] += el
                    stats[a]["n"] += 1
            rating = st.get("rating") or 0
            for a in participants:
                if rating > 0:
                    stats[a]["likes"] += 1
                elif rating < 0:
                    stats[a]["dislikes"] += 1

    def key(n):
        s = stats[n]
        total = s["done"] + s["failed"]
        success = s["done"] / total if total else 0.5
        avg = s["elapsed"] / s["n"] if s["n"] else 0.0
        return (success, s["likes"] - s["dislikes"], -avg)

    return max(cands, key=key)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an agent reply.
    strict=False: small local models sometimes emit raw control characters
    (literal newlines/tabs) inside JSON strings — tolerate them."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise AgentError(f"no JSON object found in reply: {text[:400]}")
    return json.loads(m.group(0), strict=False)


def normalize_plan(plan: dict) -> dict:
    """Make a planner's JSON safe to execute: fill missing fields, dedupe ids,
    drop depends_on edges that point at nothing, and fail with a clear message
    on a REAL dependency cycle (not a missing dep)."""
    subs = []
    seen: dict[str, int] = {}
    for i, s in enumerate(plan.get("subtasks") or []):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or f"s{i + 1}")
        seen[sid] = seen.get(sid, 0) + 1
        if seen[sid] > 1:
            sid = f"{sid}-{seen[sid]}"
        subs.append({
            "id": sid,
            "title": str(s.get("title") or sid),
            "agent": str(s.get("agent") or ""),
            "instructions": str(s.get("instructions") or s.get("title") or sid),
            "depends_on": [str(d) for d in (s.get("depends_on") or [])],
        })
    if not subs:
        raise AgentError("plan contained no usable subtasks")
    ids = {s["id"] for s in subs}
    dropped = []
    for s in subs:
        good = [d for d in dict.fromkeys(s["depends_on"]) if d in ids and d != s["id"]]
        dropped += [d for d in s["depends_on"] if d not in good]
        s["depends_on"] = good
    if dropped:
        print(f"[harness] plan referenced unknown subtasks {sorted(set(dropped))}"
              " — dropping those edges")
    # real cycle check: Kahn's algorithm
    indeg = {s["id"]: len(s["depends_on"]) for s in subs}
    dependents: dict[str, list[str]] = {s["id"]: [] for s in subs}
    for s in subs:
        for d in s["depends_on"]:
            dependents[d].append(s["id"])
    queue = [sid for sid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for dep in dependents[cur]:
            indeg[dep] -= 1
            if indeg[dep] == 0:
                queue.append(dep)
    if visited < len(subs):
        raise AgentError("dependency cycle in plan")
    plan["subtasks"] = subs
    return plan


# ---------------------------------------------------------------- context files

MAX_INLINE_FILE = 30_000  # chars per file inlined into prompts


def build_context_block(inputs: list[Path]) -> str:
    """Describe attached files; inline text contents so API-only agents can read them."""
    if not inputs:
        return ""
    lines = [
        "CONTEXT FILES attached by the user. They are also copied into your working "
        "directory under inputs/ — read or edit them there if you have file access.",
    ]
    for p in inputs:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        lines.append(f"\n--- inputs/{p.name} ({size / 1024:.1f} KB) ---")
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            lines.append("(binary file — not shown here; open it from inputs/ if you can)")
            continue
        if len(text) > MAX_INLINE_FILE:
            text = text[:MAX_INLINE_FILE] + "\n… [truncated — full file in inputs/]"
        lines.append(text)
    return "\n".join(lines) + "\n"


PROJECT_NOTES_NAMES = ("WOLFPACK.md", "AGENTS.md")


def load_project_notes(extra_dirs: list | None = None,
                       per: int = 4000, total: int = 8000) -> str:
    """Repo-native instructions: WOLFPACK.md / AGENTS.md in the harness folder
    (and in a scheduled task's output folder) are auto-included in prompts so
    conventions survive without re-pasting them into every task."""
    chunks, used = [], 0
    dirs = [HARNESS_DIR] + [Path(d) for d in (extra_dirs or []) if d]
    seen = set()
    for d in dirs:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d in seen:
            continue
        seen.add(d)
        for name in PROJECT_NOTES_NAMES:
            p = d / name
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:per]
            except OSError:
                continue
            if used + len(text) > total:
                break
            chunks.append(f"--- {name} ({d}) ---\n{text}")
            used += len(text)
    if not chunks:
        return ""
    return ("PROJECT NOTES — standing instructions from the user's notes "
            "files. Follow them unless the task says otherwise:\n"
            + "\n\n".join(chunks))


# ---------------------------------------------------------------- skills
# Skills are SKILL.md instruction packs. WolfPack scans its own skills/ folder
# plus the usual per-agent install locations, and injects enabled skills into
# worker prompts so every agent (even file-less cloud models) can use them.

def _parse_skill(text: str, fallback: str) -> tuple[str, str]:
    """(name, description) from SKILL.md frontmatter, with sane fallbacks."""
    name, desc = fallback, ""
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        body = text[m.end():]
        fm = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        if nm:
            name = nm.group(1).strip() or name
        # description may span folded YAML lines — take from its key to the next key
        dm = re.search(r"^description:\s*(.*?)(?=^[a-z_]+:|\Z)", fm,
                       re.M | re.DOTALL)
        if dm:
            desc = " ".join(dm.group(1).split())[:140]
    if not desc:
        for line in body.splitlines():
            ls = line.strip()
            if ls and not ls.startswith(("#", "---")):
                desc = ls[:140]
                break
    return name, desc


def scan_skills() -> list[dict]:
    """All discoverable skills: WolfPack's skills/ dir, agent skill dirs
    (claude / kimi / codex / Kimi desktop incl. plugins), opencode commands."""
    found: dict[str, dict] = {}
    appdata = os.environ.get("APPDATA", "")
    kimi_root = (Path(appdata) / "kimi-desktop" / "daimon-share" / "daimon"
                 if appdata else None)
    skill_dirs = [
        (HARNESS_DIR / "skills", "wolfpack"),
        (Path.home() / ".claude" / "skills", "claude"),
        (Path.home() / ".kimi" / "skills", "kimi"),
        (Path.home() / ".codex" / "skills", "codex"),
    ]
    if kimi_root:
        skill_dirs.append((kimi_root / "skills", "kimi"))
    for base, tag in skill_dirs:
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*/SKILL.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, desc = _parse_skill(text, p.parent.name)
            sid = f"{tag}:{name}"
            found[sid] = {"id": sid, "name": name, "description": desc,
                          "source": tag, "path": str(p)}
    # Kimi desktop plugin skills: plugins/managed/<plugin>/skills/<name>/SKILL.md
    if kimi_root:
        plug = kimi_root / "runtime" / "kimi-code" / "home" / "plugins" / "managed"
        if plug.is_dir():
            for p in sorted(plug.glob("*/skills/*/SKILL.md")):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name, desc = _parse_skill(text, p.parent.name)
                sid = f"kimi-plugin:{name}"
                found[sid] = {"id": sid, "name": name, "description": desc,
                              "source": "kimi-plugin", "path": str(p)}
    oc = Path.home() / ".config" / "opencode" / "command"
    if oc.is_dir():
        for p in sorted(oc.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, desc = _parse_skill(text, p.stem)
            sid = f"opencode:{name}"
            found[sid] = {"id": sid, "name": name, "description": desc,
                          "source": "opencode", "path": str(p)}
    return list(found.values())


def build_skills_block(settings: dict, per: int = 4000, total: int = 12000) -> str:
    """Inline enabled skills' instructions for worker prompts."""
    ids = set(settings.get("skills_enabled") or [])
    if not ids:
        return ""
    chunks, used = [], 0
    for sk in scan_skills():
        if sk["id"] not in ids:
            continue
        try:
            text = Path(sk["path"]).read_text(encoding="utf-8", errors="replace")[:per]
        except OSError:
            continue
        if used + len(text) > total:
            break
        chunks.append(f"--- skill: {sk['name']} (from {sk['source']}; "
                      f"files at {Path(sk['path']).parent}) ---\n{text}")
        used += len(text)
    if not chunks:
        return ""
    return ("SKILLS — when the subtask matches a skill, follow its instructions "
            "(agents with file access can read the full skill folder at the path "
            "shown):\n\n" + "\n\n".join(chunks) + "\n\n")


# ---------------------------------------------------------------- memory
# Durable cross-run memory: memory/shared/*.md is injected into every planner
# and worker prompt; memory/agents/<name>.md holds an agent's private notes.
# After each run the orchestrator gets a wrap-up prompt and can save facts.

def build_memory_block(agent: str | None = None, total: int = 6000) -> str:
    """Durable team memory as a prompt block ('' when there is none yet)."""
    if not MEMORY_DIR.is_dir():
        return ""
    chunks, used = [], 0
    shared = MEMORY_DIR / "shared"
    if shared.is_dir():
        for p in sorted(shared.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            chunk = f"--- memory: {p.stem} ---\n{text[:2000]}"
            if used + len(chunk) > total:
                break
            chunks.append(chunk)
            used += len(chunk)
    if agent:
        ap = MEMORY_DIR / "agents" / f"{agent}.md"
        if ap.is_file():
            try:
                text = ap.read_text(encoding="utf-8", errors="replace").strip()[:1500]
            except OSError:
                text = ""
            if text:
                chunks.append(f"--- your own notes from past runs ---\n{text}")
    if not chunks:
        return ""
    return ("TEAM MEMORY — durable facts learned from past runs "
            "(trusted context, keep it in mind):\n\n"
            + "\n\n".join(chunks) + "\n\n")


def save_memory_blocks(text: str) -> list[str]:
    """Parse '### memory: <slug>' sections out of an agent reply and append them
    to memory/shared/<slug>.md. Returns the slugs written."""
    written = []
    parts = re.split(r"^###\s*memory:\s*", text, flags=re.M)
    for part in parts[1:]:
        lines = part.splitlines()
        slug = re.sub(r"[^a-z0-9-]+", "-", lines[0].strip().lower()).strip("-")[:40]
        body = "\n".join(lines[1:]).strip()[:2000]
        if not slug or not body:
            continue
        d = MEMORY_DIR / "shared"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{slug}.md"
        try:
            prior = p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            prior = ""
        if body in prior:
            continue  # already remembered
        stamp = datetime.now().strftime("%Y-%m-%d")
        merged = (prior.rstrip() + f"\n\n<!-- {stamp} -->\n" + body).strip() + "\n"
        if len(merged) > 8000:  # cap: keep the newest entries
            merged = merged[-8000:].lstrip()
        p.write_text(merged, encoding="utf-8")
        written.append(slug)
    return written


def save_skill_from_text(text: str, source_note: str = "") -> str | None:
    """Write an agent-authored SKILL.md into skills/<slug>/. Returns the slug."""
    m = re.search(r"^---\s*$", text, re.M)
    body = text[m.start():].strip() if m else text.strip()
    if not body:
        return None
    name, _ = _parse_skill(body, "skill")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "skill"
    dest = HARNESS_DIR / "skills" / slug
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(body + "\n", encoding="utf-8")
    if source_note:
        (dest / "SOURCE.txt").write_text(source_note + "\n", encoding="utf-8")
    return slug


# ---------------------------------------------------------------- sandbox
# Experimental: run CLI agents inside Docker Sandboxes (docker sbx) instead of
# directly on the host. Feature-detected — hidden when Docker/sbx is absent.

_DOCKER_SBX = {"checked": False, "ok": False}


def docker_sbx_available() -> bool:
    """True when Docker with the sbx (sandboxes) plugin is present."""
    if not _DOCKER_SBX["checked"]:
        _DOCKER_SBX["checked"] = True
        ok = False
        exe = shutil.which("docker")
        if not exe:  # Docker Desktop's default install location
            cand = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
            if cand.exists():
                exe = str(cand)
        if exe:
            try:
                # full resolved path: bare "docker" won't resolve docker.cmd
                # under CreateProcess on Windows
                p = subprocess.run([exe, "sbx", "--help"],
                                   capture_output=True, timeout=10)
                ok = p.returncode == 0
            except Exception:
                ok = False
        _DOCKER_SBX["ok"] = ok
    return _DOCKER_SBX["ok"]


# ---------------------------------------------------------------- status

class RunStatus:
    """Writes status.json in the run dir so UIs can follow progress live."""

    def __init__(self, run_dir: Path, run_id: str, task: str,
                 orchestrator: str, workers: list[str]):
        self.path = run_dir / "status.json"
        self._lock = threading.Lock()
        self.state = {
            "run_id": run_id, "task": task, "orchestrator": orchestrator,
            "workers": workers, "phase": "planning",
            "started": datetime.now().isoformat(timespec="seconds"),
            "plan": None, "subtasks": {}, "error": None,
            "result_file": None,
        }
        self.save()

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)

    def set_phase(self, phase: str, error: str | None = None):
        self.state["phase"] = phase
        if error:
            self.state["error"] = error
        self.save()

    def set_plan(self, plan: dict):
        self.state["plan"] = plan
        for s in plan.get("subtasks", []):
            self.state["subtasks"][s["id"]] = {
                "title": s["title"], "agent": s["agent"],
                "depends_on": s.get("depends_on", []),
                "status": "queued", "started": None, "elapsed_sec": None,
                "preview": None,
            }
        self.save()

    def add_subtask(self, sid: str, title: str, agent: str,
                    depends_on: list[str] | None = None):
        """Register an extra subtask after planning (project mode decides
        delegations cycle by cycle)."""
        self.state["subtasks"][sid] = {
            "title": title, "agent": agent,
            "depends_on": depends_on or [],
            "status": "queued", "started": None, "elapsed_sec": None,
            "preview": None,
        }
        self.save()

    def subtask(self, sid: str, status: str, preview: str | None = None,
                started: str | None = None, elapsed: float | None = None,
                usage: dict | None = None, model: str | None = None):
        st = self.state["subtasks"].get(sid)
        if st is None:
            return
        st["status"] = status
        if started is not None:
            st["started"] = started
        if elapsed is not None:
            st["elapsed_sec"] = round(elapsed, 1)
        if preview is not None:
            st["preview"] = preview[-1500:]  # tail is the interesting part
        if usage:
            st["usage"] = usage
        if model:
            st["model"] = model
        self.save()


# ---------------------------------------------------------------- orchestration

def _cap(text: str, n: int = 20_000) -> str:
    """Cap one agent's contribution before feeding many into a synthesis
    prompt — a huge result must not blow the orchestrator's context window."""
    text = text or ""
    return text if len(text) <= n else text[:n] + "\n… [truncated]"


def inline_dep_outputs(dep_dirs: list[tuple[str, Path]],
                       cap_per_file: int = 8000,
                       cap_total: int = 24000) -> str:
    """Inline small text files from dependency workdirs — for API agents with no
    file access, so a reviewer actually sees the code it must review."""
    chunks, total = [], 0
    for d, dep in dep_dirs:
        for f in sorted(dep.iterdir()):
            if not f.is_file() or f.name == "outbox.jsonl" or f.suffix == ".log":
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError, OSError):
                continue
            text = text[:cap_per_file]
            if total + len(text) > cap_total:
                break
            chunks.append(f"--- file from {d}: {f.name} ---\n{text}")
            total += len(text)
    return "\n\n".join(chunks)


def route_inline_messages(out: str, mailbox: "Mailbox", sid: str) -> str:
    """API agents can't write outbox.jsonl — lift {"to","text"} JSON lines from
    their reply into the mailbox, and return the reply without those lines."""
    kept = []
    for line in out.splitlines():
        s = line.strip().strip("`")
        m = None
        if s.startswith("{") and '"to"' in s and '"text"' in s:
            try:
                m = json.loads(s)
            except json.JSONDecodeError:
                m = None
        if isinstance(m, dict) and "to" in m and "text" in m:
            mailbox.append(sid, str(m["to"]), str(m["text"]),
                           str(m["type"]) if m.get("type") else None)
        else:
            kept.append(line)
    return "\n".join(kept)


def run_task(task: str, orchestrator: str | None, agent_filter: list[str] | None,
             files: list[str] | None = None,
             continue_from: str | None = None,
             pattern: str = "standard",
             goal: str | None = None,
             notes_dirs: list[str] | None = None) -> Path:
    reg = load_registry()
    settings = load_settings()
    agents: dict = reg["agents"]
    if agent_filter:
        agents = {k: v for k, v in agents.items() if k in agent_filter}
    orch = orchestrator or settings.get("orchestrator") or reg.get("orchestrator", "codex")
    if orch == "auto":
        orch = (pick_auto_orchestrator(agents, settings)
                or reg.get("orchestrator", "codex"))
        print(f"[harness] auto orchestrator picked: {orch}")
    if orch not in agents and orch != "echo":
        agents[orch] = reg["agents"][orch]  # orchestrator need not be in worker pool

    # standing instructions (WOLFPACK.md / AGENTS.md) ride along with every run
    notes = load_project_notes(notes_dirs)
    if notes:
        task = task + "\n\n" + notes
    # an optional "why" — agents do visibly better unattended work with it
    if goal and goal.strip():
        task = (task.strip() + "\n\nGOAL — why this task matters (context, "
                "not extra work): " + goal.strip())

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    # avoid collisions when runs start within the same second
    n = 1
    while (RUNS_DIR / run_id).exists():
        n += 1
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{n}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # attached context files -> run_dir/inputs (strip upload-id timestamp prefix)
    inputs: list[Path] = []
    for f in files or []:
        src = Path(f)
        if not src.is_file():
            continue
        idir = run_dir / "inputs"
        idir.mkdir(exist_ok=True)
        dest = idir / re.sub(r"^\d{10,}-", "", src.name)
        shutil.copy2(src, dest)
        inputs.append(dest)

    # continuation: carry the whole thread (chain of earlier runs) over
    prev_block = ""
    if continue_from:
        chain = []  # oldest first
        cur = Path(str(continue_from)).name  # basename only
        seen = set()
        while cur and cur not in seen and len(chain) < 5:
            seen.add(cur)
            pdir = RUNS_DIR / cur
            if not pdir.is_dir():
                break
            ptask, pres, pnext = "", "", None
            pst = pdir / "status.json"
            if pst.exists():
                try:
                    pstj = json.loads(pst.read_text(encoding="utf-8"))
                    ptask = pstj.get("task", "")
                    pnext = pstj.get("continued_from")
                except json.JSONDecodeError:
                    pass
            pr = pdir / "result.md"
            if pr.exists():
                pres = pr.read_text(encoding="utf-8", errors="replace")
            chain.append((cur, ptask, pres))
            cur = Path(str(pnext)).name if pnext else None
        chain.reverse()
        parts, budget = [], 8000
        for rid, ptask, pres in chain:
            chunk = (f"--- Earlier step ({rid}) ---\nTask: {ptask}\n"
                     f"Final result:\n{pres or '(no result recorded)'}\n")
            chunk = chunk[:budget]
            parts.append(chunk)
            budget -= len(chunk)
            if budget <= 0:
                break
        if parts:
            prev_block = ("\n\nCONTEXT — this task continues an earlier "
                          "conversation thread (oldest first):\n"
                          + "\n".join(parts))
        pinputs = RUNS_DIR / chain[-1][0] / "inputs" if chain else None
        if pinputs and pinputs.is_dir():
            idir = run_dir / "inputs"
            idir.mkdir(exist_ok=True)
            for f in pinputs.iterdir():
                if f.is_file() and not (idir / f.name).exists():
                    shutil.copy2(f, idir / f.name)
                    inputs.append(idir / f.name)

    mailbox = Mailbox(run_dir / "mailbox.jsonl")
    status = RunStatus(run_dir, run_id, task, orch, list(agents))
    if pattern in ("council", "best-of-n", "project"):
        status.state["pattern"] = pattern
    if inputs:
        status.state["files"] = [p.name for p in inputs]
    if continue_from:
        status.state["continued_from"] = Path(str(continue_from)).name
    if goal and goal.strip():
        status.state["goal"] = goal.strip()

    # session resume: a follow-up run reuses the previous run's orchestrator
    # CLI session (same agent, resume_command configured) so it keeps its
    # working context instead of starting cold
    resume_dir = None
    if continue_from:
        prev_base = RUNS_DIR / Path(str(continue_from)).name
        prev_orch = prev_base / "orchestrator"
        try:
            pst = json.loads((prev_base / "status.json").read_text(encoding="utf-8"))
            same_orch = pst.get("orchestrator") == orch
        except Exception:
            same_orch = False
        ocfg = agents.get(orch) or reg["agents"].get(orch) or {}
        if (same_orch and prev_orch.is_dir() and ocfg.get("type") == "cli"
                and ocfg.get("resume_command")):
            resume_dir = prev_orch
            print(f"[harness] follow-up: resuming {orch}'s previous session")
    if inputs or continue_from or pattern != "standard" or goal:
        status.save()

    print(f"[harness] run {run_id}  orchestrator={orch}  workers={list(agents)}"
          + (f"  files={[p.name for p in inputs]}" if inputs else "")
          + (f"  continues={continue_from}" if continue_from else ""))

    RUN_CANCEL[run_id] = threading.Event()
    try:
        _run_task_inner(task, orch, agents, reg, run_dir, mailbox, status, settings,
                        inputs, prev_block, resume_dir)
    except AgentCancelled:
        status.set_phase("stopped", "stopped by user")
        print(f"[harness] run {run_id} stopped by user")
    except Exception as e:
        status.set_phase("failed", str(e))
        raise
    finally:
        RUN_CANCEL.pop(run_id, None)
    return run_dir


def _run_task_inner(task, orch, agents, reg, run_dir, mailbox, status, settings,
                    inputs, prev_block="", resume_dir: Path | None = None):
    cancel = RUN_CANCEL.get(status.state["run_id"])

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()
    context = build_context_block(inputs)

    # council / best-of-n / project: non-DAG patterns with their own loops
    pattern = status.state.get("pattern") or "standard"
    if pattern in ("council", "best-of-n"):
        return _run_council(pattern, task, orch, agents, reg, run_dir, mailbox,
                            status, settings, inputs, prev_block)
    if pattern == "project":
        return _run_project(task, orch, agents, reg, run_dir, mailbox,
                            status, settings, inputs, prev_block)

    # 1. plan ----------------------------------------------------------
    memory_on = settings.get("memory_enabled", True)
    memory_block = build_memory_block(orch) if memory_on else ""
    roster_items = list(agents.items())
    roster_note = ""
    if len(roster_items) > 12:  # keep the planner's roster compact
        roster_items = sorted(
            roster_items,
            key=lambda kv: 0 if agent_tier(kv[0], kv[1], settings) == "strong"
            else 1)[:12]
        roster_note = f"\n(+{len(agents) - 12} more agents configured)"
    roster = "\n".join(
        f"- {n}: {c.get('description','')} (strengths: {', '.join(c.get('strengths', []))}; tier: {agent_tier(n, c, settings)}; status: {c.get('status','?')})"
        for n, c in roster_items
    ) + roster_note
    task_full = task + prev_block + ("\n\n" + context if context else "")
    worker_context = (prev_block + "\n" + context).strip() + ("\n" if (prev_block or context) else "")
    skills_block = build_skills_block(settings)
    enabled = set(settings.get("skills_enabled", []))
    sk_names = ", ".join(f"{s['name']} ({s['source']})" for s in scan_skills()
                         if s["id"] in enabled) or "none"
    orch_dir = run_dir / "orchestrator"

    # planning is the most failure-prone step in a multi-agent run — retry once
    # with a different strong-tier agent when the primary planner's reply isn't
    # a valid plan
    planners = [orch]
    alt = pick_review_agent(orch, agents, settings)
    if alt:
        planners.append(alt[0])
    else:
        planners.append(orch)  # no alternative: retry the same planner once
    plan = None
    plan_err: Exception | None = None
    for attempt, pname in enumerate(planners):
        if cancelled():
            raise AgentCancelled("stopped by user")
        pcfg = agents[pname] if pname in agents else reg["agents"][pname]
        slog = orch_dir / ("plan.stream.log" if attempt == 0 else "plan2.stream.log")
        prompt = PLAN_PROMPT.format(agent_roster=roster, task=task_full,
                                    skills=sk_names, memory_block=memory_block)
        if attempt > 0:
            prompt += ("\n\nIMPORTANT: the previous planning reply was not valid "
                       "JSON. Reply with ONLY the JSON object — no prose, no "
                       "code fences; double-check every comma and quote.")
        try:
            plan_raw = invoke_agent(pname, pcfg, prompt,
                                    resume_dir or orch_dir, stream_file=slog,
                                    cancel_event=cancel,
                                    resume=bool(resume_dir) and pname == orch)
            record_usage(status, "plan", take_call_meta(), agent=pname)
            plan = normalize_plan(extract_json(plan_raw))
            if pname != orch:
                print(f"[harness] planner failover: {pname} produced the plan")
                status.state["planner"] = pname
            break
        except AgentCancelled:
            raise
        except Exception as e:
            plan_err = e
            print(f"[harness] planning with {pname} failed: {e}")
    if plan is None:
        raise AgentError(f"planning failed: {plan_err}")
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    subtasks = {s["id"]: s for s in plan["subtasks"]}
    status.set_plan(plan)
    status.set_phase("executing")
    print(f"[harness] plan: {[(s['id'], s['agent'], s['title']) for s in plan['subtasks']]}")

    # 2. execute DAG ----------------------------------------------------
    results: dict[str, str] = {}
    done = threading.Event()
    lock = threading.Lock()

    def ready(s):
        return all(d in results for d in s.get("depends_on", []))

    def execute(s):
        sid, name = s["id"], s["agent"]
        workdir = run_dir / "agents" / sid
        if cancelled():
            with lock:
                results[sid] = "Stopped by user before this step started."
            status.subtask(sid, "stopped", preview=results[sid])
            return
        if name not in agents:
            with lock:
                results[sid] = f"SUBTASK FAILED: plan assigned unknown agent '{name}'"
            status.subtask(sid, "failed", preview=results[sid])
            print(f"[harness] <- {sid} FAILED: unknown agent '{name}'")
            return
        prior = "\n".join(
            f"--- Result of {d} ({subtasks[d]['title']}) ---\n{results[d]}"
            for d in s.get("depends_on", []) if d in results
        )
        # hand off actual artifacts: files from dependency workdirs
        is_api = agents[name].get("type") == "api"
        dep_dirs = []
        for d in s.get("depends_on", []):
            dep = run_dir / "agents" / d
            if dep.is_dir() and any(dep.iterdir()):
                dep_dirs.append((d, dep))
                if not is_api:  # CLI agents get real copies in their workdir
                    shutil.copytree(dep, workdir / "prior_work" / d,
                                    dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(
                                        "outbox.jsonl", "*.stream.log",
                                        "prior_work", "inputs"))
        prior_txt = ""
        if prior:
            prior_txt = "Completed prior work you can build on:\n" + prior
            if dep_dirs and not is_api:
                prior_txt += ("\nFiles produced by those subtasks are copied into your "
                              "working directory under prior_work/<subtask_id>/ — "
                              "read or improve them there.\n")
            if dep_dirs and is_api:
                inline = inline_dep_outputs(dep_dirs)
                if inline:
                    prior_txt += "\nContents of key files from prior subtasks:\n" + inline + "\n"
        failed_deps = [d for d in s.get("depends_on", [])
                       if str(results.get(d, "")).startswith("SUBTASK FAILED")]
        if failed_deps:
            prior_txt = ("NOTE: these earlier steps FAILED — do your best without "
                         "their output and say what you had to assume: "
                         + ", ".join(failed_deps) + "\n\n" + prior_txt)
        if inputs:  # give CLI agents real file access in their workdir
            shutil.copytree(run_dir / "inputs", workdir / "inputs",
                            dirs_exist_ok=True)
        prompt = SUBTASK_PROMPT.format(
            agent=name, title=s["title"], instructions=s["instructions"],
            mailbox=mailbox.render(),
            prior_results=prior_txt,
            memory_block=build_memory_block(name) if memory_on else "",
            context_files=worker_context,
            skills_block=skills_block,
            file_access=FILE_ACCESS_API if is_api else FILE_ACCESS_CLI,
        )
        print(f"[harness] -> {sid} to {name}: {s['title']}")
        t0 = time.time()
        status.subtask(sid, "running",
                       started=datetime.now().isoformat(timespec="seconds"))
        stream = run_dir / "agents" / f"{sid}.stream.log"
        meta = None
        actual = name
        try:
            out = invoke_agent(actual, agents[actual], prompt, workdir,
                               stream_file=stream, cancel_event=cancel)
            if is_api:  # no outbox file — route inline messages instead
                out = route_inline_messages(out, mailbox, sid)
            meta = take_call_meta()
            record_usage(status, sid, meta, agent=actual)
            st = "done"
        except AgentCancelled:
            out = "Stopped by user."
            st = "stopped"
        except Exception as e:  # one rate limit shouldn't sink the step
            alt = pick_failover_agent(actual, agents, settings)
            if alt:
                print(f"[harness] {sid}: {actual} failed ({e}) — failover to {alt}")
                try:
                    with stream.open("a", encoding="utf-8") as sf:
                        sf.write(f"\n[{actual} failed: {e} — failover to {alt}]\n")
                except OSError:
                    pass
                try:
                    out = invoke_agent(alt, agents[alt], prompt, workdir,
                                       stream_file=stream, cancel_event=cancel)
                    if agents[alt].get("type") == "api":
                        out = route_inline_messages(out, mailbox, sid)
                    meta = take_call_meta()
                    record_usage(status, sid, meta, agent=alt)
                    actual = alt
                    st = "done"
                except AgentCancelled:
                    out = "Stopped by user."
                    st = "stopped"
                except Exception as e2:
                    out = f"SUBTASK FAILED: {e} (failover {alt} also failed: {e2})"
                    st = "failed"
            else:
                out = f"SUBTASK FAILED: {e}"
                st = "failed"
        mailbox.merge_outbox(sid, workdir)
        with lock:
            if sid in results:
                # this worker exceeded the batch time cap and was already
                # written off as failed — don't let the late finish rewrite it
                print(f"[harness] <- {sid} finished late; keeping failed status")
                return
            results[sid] = out
        status.subtask(sid, st, preview=out, elapsed=time.time() - t0,
                       usage=(meta or {}).get("usage"),
                       model=(meta or {}).get("model"))
        if actual != name:
            status.state["subtasks"][sid]["failover"] = actual
            status.save()

    pending = list(plan["subtasks"])
    max_par = max(1, settings.get("max_parallel", 5))
    while pending:
        if cancelled():
            for s in pending:
                results[s["id"]] = "Stopped by user before this step started."
                status.subtask(s["id"], "stopped", preview=results[s["id"]])
            break
        batch = [s for s in pending if ready(s)][:max_par]
        if not batch:
            raise AgentError("dependency cycle in plan")
        # a hung worker must not stall the whole run: cap each batch at the
        # slowest agent's timeout (+2 min grace) and mark stragglers failed
        pool = ThreadPoolExecutor(max_workers=len(batch))
        futs = {pool.submit(execute, s): s for s in batch}
        cap = max(int((agents.get(s["agent"]) or {}).get("timeout_sec", 900))
                  for s in batch) + 120
        done_f, strays = wait(futs, timeout=cap)
        pool.shutdown(wait=False, cancel_futures=True)
        for f in done_f:
            f.result()
        for f in strays:
            sid = futs[f]["id"]
            with lock:
                if sid not in results:
                    results[sid] = (f"SUBTASK FAILED: exceeded the harness time "
                                    f"limit ({cap}s)")
            status.subtask(sid, "failed", preview=results[sid])
            print(f"[harness] <- {sid} FAILED: harness time limit {cap}s")
        pending = [s for s in pending if s["id"] not in results]
        _maybe_wait_for_approval(mailbox, run_dir, status, cancel)
        _budget_check(status, settings, mailbox, run_dir, cancel)

    (run_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                          encoding="utf-8")

    if cancelled():
        status.set_phase("stopped", "stopped by user")
        print(f"[harness] run {status.state['run_id']} stopped by user")
        return run_dir

    # 3. synthesize ------------------------------------------------------
    status.set_phase("synthesizing")
    results_md = "\n\n".join(
        f"## {sid}: {subtasks[sid]['title']} (agent: {subtasks[sid]['agent']})\n{_cap(results[sid])}"
        for sid in results
    )
    # synthesis is the other single point of failure: if the orchestrator is
    # rate-limited here, all the finished subtask work would be wasted —
    # retry once with a different strong-tier agent, like planning does
    synthesizers = [orch]
    salt = pick_review_agent(orch, agents, settings)
    if salt:
        synthesizers.append(salt[0])
    final = None
    synth_err: Exception | None = None
    for attempt, sname in enumerate(synthesizers):
        if cancelled():
            raise AgentCancelled("stopped by user")
        scfg = agents[sname] if sname in agents else reg["agents"][sname]
        try:
            final = invoke_agent(
                sname, scfg,
                SYNTH_PROMPT.format(task=task_full,
                                    guidance=plan.get("synthesis_guidance", ""),
                                    results=results_md,
                                    mailbox=mailbox.render()),
                resume_dir or orch_dir,
                stream_file=orch_dir / ("synth.stream.log" if attempt == 0
                                        else "synth2.stream.log"),
                cancel_event=cancel,
                resume=bool(resume_dir) and sname == orch)
            record_usage(status, "synthesize", take_call_meta(), agent=sname)
            if sname != orch:
                print(f"[harness] synthesis failover: {sname} wrote the result")
                status.state["synthesizer"] = sname
            break
        except AgentCancelled:
            raise
        except Exception as e:
            synth_err = e
            print(f"[harness] synthesis with {sname} failed: {e}")
    if final is None:
        raise AgentError(f"synthesis failed: {synth_err}")
    (run_dir / "result.md").write_text(final, encoding="utf-8")

    # 4. review pass + memory wrap-up (shared with council mode)
    final = _final_review(task_full, final, orch, agents, reg, orch_dir, run_dir,
                          status, settings, cancel)
    _memory_wrapup(task_full, final, orch, agents, reg, orch_dir, run_dir,
                   status, settings, cancel)

    if cancelled():
        status.set_phase("stopped", "stopped by user")
        print(f"[harness] run {status.state['run_id']} stopped by user")
        return run_dir

    status.state["result_file"] = "result.md"
    status.set_phase("done")
    print(f"[harness] final result -> {run_dir / 'result.md'}")
    return run_dir


def _final_review(task_full: str, final: str, orch: str, agents: dict, reg: dict,
                  orch_dir: Path, run_dir: Path, status: RunStatus,
                  settings: dict, cancel) -> str:
    """Optional final review: a different strong agent critiques the result and
    the orchestrator revises it. Returns the (possibly improved) final result."""
    if not settings.get("review_pass", True):
        return final
    if cancel is not None and cancel.is_set():
        return final
    rev = pick_review_agent(orch, agents, settings)
    if not rev:
        return final
    rname, rcfg = rev
    status.set_phase("reviewing")
    status.state["review"] = {"agent": rname}
    status.save()
    print(f"[harness] review pass by {rname}")
    critique = ""
    try:
        critique = invoke_agent(rname, rcfg,
                                REVIEW_PROMPT.format(task=task_full, result=final),
                                orch_dir,
                                stream_file=orch_dir / "review.stream.log",
                                cancel_event=cancel)
        record_usage(status, "review", take_call_meta(), agent=rname)
    except AgentCancelled:
        return final
    except Exception as e:
        status.state["review"] = {"agent": rname, "error": str(e)}
        status.save()
        print(f"[harness] review failed ({e}) — keeping original result")
        return final
    if critique:
        (run_dir / "review.md").write_text(critique, encoding="utf-8")
        verdict = "revise" if re.search(r"VERDICT:\s*REVISE", critique, re.I) \
                  else "pass"
        status.state["review"] = {"agent": rname, "verdict": verdict}
        status.save()
        if verdict == "revise" and not (cancel is not None and cancel.is_set()):
            print("[harness] reviewer requested changes — revising")
            final = invoke_agent(
                orch, agents[orch] if orch in agents else reg["agents"][orch],
                REVISE_PROMPT.format(task=task_full, result=final,
                                     critique=critique),
                orch_dir, stream_file=orch_dir / "revise.stream.log",
                cancel_event=cancel)
            record_usage(status, "revise", take_call_meta(), agent=orch)
            (run_dir / "result.md").write_text(final, encoding="utf-8")
    return final


def _verify_project(task_full: str, final: str, orch: str, agents: dict,
                    reg: dict, run_dir: Path, proj_dir: Path,
                    test_report: dict, status: RunStatus, settings: dict,
                    cancel) -> None:
    """Done-verification pass (project mode): a different strong agent checks
    the 'done' claim against what is actually on disk — files exist and are
    non-trivial, tests really ran. Appends a verification section to result.md."""
    if not settings.get("verify_pass", True):
        return
    if cancel is not None and cancel.is_set():
        return
    rev = pick_review_agent(orch, agents, settings)
    if not rev:
        return
    vname, vcfg = rev
    status.set_phase("verifying")
    print(f"[harness] verification pass by {vname}")
    skip_dirs = ("prior_work", "inputs", "__pycache__", "node_modules",
                 ".venv", ".git")
    lines: list[str] = []
    try:
        for p in sorted(proj_dir.rglob("*")):
            if not p.is_file() or len(lines) >= 120:
                continue
            rel = p.relative_to(proj_dir)
            if any(part in skip_dirs for part in rel.parts):
                continue
            lines.append(f"{rel}  ({p.stat().st_size} bytes)")
    except OSError:
        pass
    evidence = ("Project folder listing:\n"
                + ("\n".join(lines) or "(EMPTY — no files were produced!)")
                + "\n\nLast automated test report:\n"
                + json.dumps(test_report, ensure_ascii=False)[:1500])
    try:
        raw = invoke_agent(vname, vcfg,
                           VERIFY_PROMPT.format(task=task_full[:4000],
                                                report=final[-8000:],
                                                evidence=evidence),
                           run_dir / "orchestrator",
                           stream_file=run_dir / "orchestrator" / "verify.stream.log",
                           cancel_event=cancel)
        record_usage(status, "verify", take_call_meta(), agent=vname)
    except AgentCancelled:
        return
    except Exception as e:
        print(f"[harness] verification failed ({e}) — result stands unverified")
        status.state["verification"] = {"agent": vname, "error": str(e)}
        status.save()
        return
    try:
        v = extract_json(raw)
        verdict = "pass" if str(v.get("verdict", "")).lower().startswith("pass") \
                  else "concerns"
        notes = str(v.get("notes") or "")
    except AgentError:
        verdict, notes = "concerns", raw[:500]
    status.state["verification"] = {"agent": vname, "verdict": verdict,
                                    "notes": notes}
    status.save()
    (run_dir / "verification.md").write_text(
        f"Verdict: {verdict}\nVerifier: {vname}\n\n{notes}\n", encoding="utf-8")
    if notes:
        with open(run_dir / "result.md", "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## Verification ({vname})\n\n"
                    f"**{'✓ verified' if verdict == 'pass' else '⚠ concerns'}**"
                    f" — {notes}\n")
    print(f"[harness] verification: {verdict}")


def _memory_wrapup(task_full: str, final: str, orch: str, agents: dict, reg: dict,
                   orch_dir: Path, run_dir: Path, status: RunStatus,
                   settings: dict, cancel) -> None:
    """Last step of a run: the orchestrator may save durable facts to team memory."""
    if not settings.get("memory_enabled", True):
        return
    if cancel is not None and cancel.is_set():
        return
    try:
        status.set_phase("learning")
        out = invoke_agent(
            orch, agents[orch] if orch in agents else reg["agents"][orch],
            MEMORY_PROMPT.format(task=task_full[:4000], result=final[:8000]),
            orch_dir, stream_file=orch_dir / "memory.stream.log",
            cancel_event=cancel)
        record_usage(status, "memory", take_call_meta(), agent=orch)
        written = save_memory_blocks(out)
        if written:
            status.state["memory"] = written
            status.save()
            print(f"[harness] memory updated: {', '.join(written)}")
    except AgentCancelled:
        pass
    except Exception as e:
        print(f"[harness] memory step failed ({e}) — continuing")


def _maybe_wait_for_approval(mailbox: Mailbox, run_dir: Path,
                             status: RunStatus, cancel) -> None:
    """Approval gate: when an agent posts a blocking question (a mailbox message
    with type="approval"), pause the run until the user answers from the mailbox
    (the server writes approval-response.json into the run dir)."""
    pend = run_dir / "pending_question.json"
    resp = run_dir / "approval-response.json"
    approvals = [m for m in mailbox.all() if m.get("type") == "approval"]
    if not approvals:
        return
    last = approvals[-1]
    if status.state.get("answered_approval") == last.get("ts"):
        return  # this question was already answered
    q = {"from": last.get("from"), "text": last.get("text"), "ts": last.get("ts")}
    pend.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    prev_phase = status.state.get("phase") or "executing"
    status.set_phase("waiting")
    print(f"[harness] run paused — waiting for user answer: "
          f"{str(last.get('text', ''))[:120]}")
    try:
        while not resp.exists():
            if cancel is not None and cancel.is_set():
                raise AgentCancelled("stopped by user")
            time.sleep(2)
        try:
            answer = json.loads(resp.read_text(encoding="utf-8")).get("answer", "")
        except json.JSONDecodeError:
            answer = resp.read_text(encoding="utf-8", errors="replace")
        mailbox.append("user", str(last.get("from") or "all"),
                       f"[answer to your question] {answer}")
        status.state["answered_approval"] = last.get("ts")
        status.save()
        print("[harness] user answered — resuming run")
    finally:
        pend.unlink(missing_ok=True)
        resp.unlink(missing_ok=True)
        if status.state.get("phase") == "waiting":
            status.set_phase(prev_phase if prev_phase != "waiting" else "executing")


def _budget_check(status: RunStatus, settings: dict, mailbox: Mailbox,
                  run_dir: Path, cancel) -> None:
    """Soft budget: when a run passes N agent calls (Settings, 0 = off), pause
    with an approval question so paid subscriptions don't burn unattended.
    A per-agent cap (agent_call_cap) does the same when ONE agent crosses its
    own call count — catches a single stuck agent looping."""
    budget = int(settings.get("budget_calls", 0) or 0)
    cap = int(settings.get("agent_call_cap", 0) or 0)
    calls = (status.state.get("usage") or {}).get("calls", 0)
    over_global = budget and calls >= (status.state.get("budget_next") or budget)
    over_agent = None
    if cap:
        for aname, pa in (status.state.get("usage_by_agent") or {}).items():
            tripped = (status.state.get("cap_tripped") or {})
            if pa.get("calls", 0) >= cap and tripped.get(aname, 0) < pa["calls"]:
                over_agent = (aname, pa["calls"])
                break
    if not over_global and not over_agent:
        return
    if over_agent:
        aname, acalls = over_agent
        print(f"[harness] per-agent cap reached ({aname}: {acalls} calls) — asking")
        mailbox.append("harness", "user",
                       f"Agent '{aname}' alone has made {acalls} calls in this "
                       f"run (your per-agent cap is {cap}). Reply anything to "
                       f"let it continue, or press ■ stop to end the run here.",
                       mtype="approval")
    else:
        print(f"[harness] budget reached ({calls} calls) — asking the user")
        mailbox.append("harness", "user",
                       f"This run has made {calls} agent calls (your budget is "
                       f"{budget}). Reply anything to keep going for another "
                       f"{budget} calls, or press ■ stop to end it here.",
                       mtype="approval")
    _maybe_wait_for_approval(mailbox, run_dir, status, cancel)
    if over_agent:
        tripped = status.state.setdefault("cap_tripped", {})
        tripped[over_agent[0]] = over_agent[1]
    else:
        status.state["budget_next"] = calls + budget
    status.save()


def _run_council(pattern: str, task: str, orch: str, agents: dict, reg: dict,
                 run_dir: Path, mailbox: Mailbox, status: RunStatus,
                 settings: dict, inputs: list[Path], prev_block: str) -> Path:
    """Council / best-of-n: every selected agent answers the raw task in
    parallel, then the orchestrator judges the answers. No planning step —
    the pattern IS the plan."""
    cancel = RUN_CANCEL.get(status.state["run_id"])

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    context = build_context_block(inputs)
    skills_block = build_skills_block(settings)
    memory_on = settings.get("memory_enabled", True)
    task_full = task + prev_block + ("\n\n" + context if context else "")
    worker_context = (prev_block + "\n" + context).strip() + ("\n" if (prev_block or context) else "")
    members = [n for n, c in agents.items() if c.get("type") != "echo"]
    if not members:
        members = list(agents)  # echo-only pool still works (testing)
    members = members[: max(2, settings.get("max_parallel", 5))]
    plan = {"subtasks": [
        {"id": f"c{i + 1}", "title": f"{n}'s answer", "agent": n,
         "instructions": task, "depends_on": []}
        for i, n in enumerate(members)],
        "synthesis_guidance": f"{pattern}: judge the parallel answers"}
    status.set_plan(plan)
    status.set_phase("executing")
    print(f"[harness] {pattern}: members={members}")

    results: dict[str, str] = {}
    lock = threading.Lock()

    def answer(s):
        sid, name = s["id"], s["agent"]
        if cancelled():
            with lock:
                results[sid] = "Stopped by user before this step started."
            status.subtask(sid, "stopped", preview=results[sid])
            return
        workdir = run_dir / "agents" / sid
        prompt = SUBTASK_PROMPT.format(
            agent=name, title=s["title"],
            instructions=s["instructions"] + "\n\n" + COUNCIL_MEMBER_NOTE,
            mailbox=mailbox.render(), prior_results="",
            memory_block=build_memory_block(name) if memory_on else "",
            context_files=worker_context,
            skills_block=skills_block,
            file_access=FILE_ACCESS_API if agents[name].get("type") == "api"
            else FILE_ACCESS_CLI)
        print(f"[harness] -> {sid} to {name} ({pattern})")
        t0 = time.time()
        status.subtask(sid, "running",
                       started=datetime.now().isoformat(timespec="seconds"))
        stream = run_dir / "agents" / f"{sid}.stream.log"
        meta = None
        actual = name
        try:
            out = invoke_agent(actual, agents[actual], prompt, workdir,
                               stream_file=stream, cancel_event=cancel)
            if agents[actual].get("type") == "api":
                out = route_inline_messages(out, mailbox, sid)
            meta = take_call_meta()
            record_usage(status, sid, meta, agent=actual)
            st = "done"
        except AgentCancelled:
            out, st = "Stopped by user.", "stopped"
        except Exception as e:
            alt = pick_failover_agent(actual, agents, settings)
            if alt:
                print(f"[harness] {sid}: {actual} failed ({e}) — failover to {alt}")
                try:
                    with stream.open("a", encoding="utf-8") as sf:
                        sf.write(f"\n[{actual} failed: {e} — failover to {alt}]\n")
                except OSError:
                    pass
                try:
                    out = invoke_agent(alt, agents[alt], prompt, workdir,
                                       stream_file=stream, cancel_event=cancel)
                    if agents[alt].get("type") == "api":
                        out = route_inline_messages(out, mailbox, sid)
                    meta = take_call_meta()
                    record_usage(status, sid, meta, agent=alt)
                    actual = alt
                    st = "done"
                except AgentCancelled:
                    out, st = "Stopped by user.", "stopped"
                except Exception as e2:
                    out = f"SUBTASK FAILED: {e} (failover {alt} also failed: {e2})"
                    st = "failed"
            else:
                out, st = f"SUBTASK FAILED: {e}", "failed"
        mailbox.merge_outbox(sid, workdir)
        with lock:
            results[sid] = out
        status.subtask(sid, st, preview=out, elapsed=time.time() - t0,
                       usage=(meta or {}).get("usage"),
                       model=(meta or {}).get("model"))
        if actual != name:
            status.state["subtasks"][sid]["failover"] = actual
            status.save()
        print(f"[harness] <- {sid} done in {time.time()-t0:.0f}s")

    pool = ThreadPoolExecutor(max_workers=len(plan["subtasks"]))
    futs = {pool.submit(answer, s): s for s in plan["subtasks"]}
    cap = max(int((agents.get(s["agent"]) or {}).get("timeout_sec", 900))
              for s in plan["subtasks"]) + 120
    done_f, strays = wait(futs, timeout=cap)
    pool.shutdown(wait=False, cancel_futures=True)
    for f in done_f:
        f.result()
    for f in strays:
        sid = futs[f]["id"]
        with lock:
            if sid not in results:
                results[sid] = (f"SUBTASK FAILED: exceeded the harness time "
                                f"limit ({cap}s)")
        status.subtask(sid, "failed", preview=results[sid])

    (run_dir / "results.json").write_text(json.dumps(results, indent=2,
                                                     ensure_ascii=False),
                                          encoding="utf-8")
    if cancelled():
        status.set_phase("stopped", "stopped by user")
        return run_dir
    _maybe_wait_for_approval(mailbox, run_dir, status, cancel)
    _budget_check(status, settings, mailbox, run_dir, cancel)

    # judge --------------------------------------------------------------
    status.set_phase("synthesizing")
    answers_md = "\n\n".join(
        f"--- draft {s['id']} by {s['agent']} ---\n{_cap(results.get(s['id'], '(no answer)'))}"
        for s in plan["subtasks"])
    judge_prompt = COUNCIL_PROMPT if pattern == "council" else BESTOF_PROMPT
    orch_dir = run_dir / "orchestrator"
    judges = [orch]
    jalt = pick_review_agent(orch, agents, settings)
    if jalt:
        judges.append(jalt[0])
    final = None
    judge_err: Exception | None = None
    for attempt, jname in enumerate(judges):
        if cancelled():
            raise AgentCancelled("stopped by user")
        jcfg = agents[jname] if jname in agents else reg["agents"][jname]
        try:
            final = invoke_agent(
                jname, jcfg,
                judge_prompt.format(task=task_full, answers=answers_md)
                + "\n" + SCORE_NOTE,
                orch_dir,
                stream_file=orch_dir / ("synth.stream.log" if attempt == 0
                                        else "synth2.stream.log"),
                cancel_event=cancel)
            record_usage(status, "judge", take_call_meta(), agent=jname)
            if jname != orch:
                print(f"[harness] judge failover: {jname} wrote the verdict")
                status.state["synthesizer"] = jname
            break
        except AgentCancelled:
            raise
        except Exception as e:
            judge_err = e
            print(f"[harness] judging with {jname} failed: {e}")
    if final is None:
        raise AgentError(f"judging failed: {judge_err}")
    final, scores = _parse_scores(final)
    if scores:
        status.state["scores"] = scores
        status.save()
    (run_dir / "result.md").write_text(final, encoding="utf-8")

    final = _final_review(task_full, final, orch, agents, reg, orch_dir, run_dir,
                          status, settings, cancel)
    _memory_wrapup(task_full, final, orch, agents, reg, orch_dir, run_dir,
                   status, settings, cancel)
    if cancelled():
        status.set_phase("stopped", "stopped by user")
        return run_dir
    status.state["result_file"] = "result.md"
    status.set_phase("done")
    print(f"[harness] final result -> {run_dir / 'result.md'}")
    return run_dir


def _run_project(task: str, orch: str, agents: dict, reg: dict,
                 run_dir: Path, mailbox: Mailbox, status: RunStatus,
                 settings: dict, inputs: list[Path], prev_block: str) -> Path:
    """Project mode: the lead agent builds a real project in run_dir/project —
    plan, build, test, fix — looping up to max_cycles times. Small side work
    (research, boilerplate, copy) is delegated to cheap agents each cycle."""
    cancel = RUN_CANCEL.get(status.state["run_id"])

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    context = build_context_block(inputs)
    memory_on = settings.get("memory_enabled", True)
    task_full = task + prev_block + ("\n\n" + context if context else "")
    max_cycles = 3

    # the builder needs real file access: the orchestrator if it's a CLI agent,
    # otherwise the first ready CLI agent in the pool
    def is_cli(n: str) -> bool:
        cfg = agents.get(n) or reg["agents"].get(n) or {}
        return cfg.get("type") == "cli" and n != "echo" \
            and cfg.get("status") in ("ready", "degraded")
    builder = orch if is_cli(orch) else next(
        (n for n in agents if is_cli(n)), None)
    if not builder:
        raise AgentError(
            "project mode needs an installed CLI agent (codex, opencode, "
            "claude) to build and test files — none is ready")
    status.state["builder"] = builder

    proj_dir = run_dir / "project"
    proj_dir.mkdir(exist_ok=True)
    if inputs:
        idir = proj_dir / "inputs"
        idir.mkdir(exist_ok=True)
        for f in inputs:
            if f.is_file() and not (idir / f.name).exists():
                shutil.copy2(f, idir / f.name)

    # 1. plan ---------------------------------------------------------------
    status.set_phase("planning")
    roster = "\n".join(
        f"- {n}: {c.get('description','')} (tier: {agent_tier(n, c, settings)}; "
        f"status: {c.get('status','?')})"
        for n, c in agents.items() if n != builder)
    memory_block = build_memory_block(orch) if memory_on else ""
    plan_raw = invoke_agent(
        orch, agents[orch] if orch in agents else reg["agents"][orch],
        PROJECT_PLAN_PROMPT.format(task=task_full, agent_roster=roster or "(none)",
                                   memory_block=memory_block,
                                   max_cycles=max_cycles),
        run_dir / "orchestrator",
        stream_file=run_dir / "orchestrator" / "plan.stream.log",
        cancel_event=cancel)
    record_usage(status, "plan", take_call_meta(), agent=orch)
    (run_dir / "plan-raw.md").write_text(plan_raw, encoding="utf-8")
    try:
        p = extract_json(plan_raw)
    except AgentError:
        p = {"architecture": plan_raw[:1500], "delegate": [], "first_steps": ""}
    architecture = str(p.get("architecture") or "")
    status.state["plan"] = {"architecture": architecture[:500],
                            "subtasks": []}
    status.save()
    print(f"[harness] project: builder={builder} arch={architecture[:80]!r}")

    orch_dir = run_dir / "orchestrator"
    results_lock = threading.Lock()
    cycle_log: list[str] = []
    build_notes = ""
    test_report: dict = {"tests_run": "", "bugs": [], "status": "fail"}

    def delegate(s, cycle: int) -> tuple[str, str]:
        """Run one delegated side task; returns (sid, output)."""
        sid, name = s["id"], s["agent"]
        if name not in agents or name == builder:
            name = next((n for n in agents
                         if agent_tier(n, agents[n], settings) == "fast"
                         and n != builder), None) or builder
        sid = f"d{cycle}-{sid}" if not str(sid).startswith("d") else str(sid)
        title = str(s.get("title") or sid)
        status.add_subtask(sid, title, name)
        if cancelled():
            status.subtask(sid, "stopped", preview="Stopped by user.")
            return sid, "Stopped by user."
        workdir = run_dir / "agents" / sid
        prompt = SUBTASK_PROMPT.format(
            agent=name, title=title,
            instructions=str(s.get("instructions") or title),
            mailbox=mailbox.render(), prior_results="",
            memory_block=build_memory_block(name) if memory_on else "",
            context_files="",
            skills_block=build_skills_block(settings),
            file_access=FILE_ACCESS_API if agents[name].get("type") == "api"
            else FILE_ACCESS_CLI)
        print(f"[harness] project -> {sid} to {name}")
        t0 = time.time()
        status.subtask(sid, "running",
                       started=datetime.now().isoformat(timespec="seconds"))
        stream = run_dir / "agents" / f"{sid}.stream.log"
        meta = None
        actual = name
        try:
            out = invoke_agent(actual, agents[actual], prompt, workdir,
                               stream_file=stream, cancel_event=cancel)
            if agents[actual].get("type") == "api":
                out = route_inline_messages(out, mailbox, sid)
            meta = take_call_meta()
            record_usage(status, sid, meta, agent=actual)
            st = "done"
        except AgentCancelled:
            out, st = "Stopped by user.", "stopped"
        except Exception as e:
            alt = pick_failover_agent(actual, agents, settings)
            if alt:
                print(f"[harness] {sid}: {actual} failed ({e}) — failover to {alt}")
                try:
                    with stream.open("a", encoding="utf-8") as sf:
                        sf.write(f"\n[{actual} failed: {e} — failover to {alt}]\n")
                except OSError:
                    pass
                try:
                    out = invoke_agent(alt, agents[alt], prompt, workdir,
                                       stream_file=stream, cancel_event=cancel)
                    if agents[alt].get("type") == "api":
                        out = route_inline_messages(out, mailbox, sid)
                    meta = take_call_meta()
                    record_usage(status, sid, meta, agent=alt)
                    actual = alt
                    st = "done"
                except AgentCancelled:
                    out, st = "Stopped by user.", "stopped"
                except Exception as e2:
                    out = f"SUBTASK FAILED: {e} (failover {alt} also failed: {e2})"
                    st = "failed"
            else:
                out, st = f"SUBTASK FAILED: {e}", "failed"
        mailbox.merge_outbox(sid, workdir)
        status.subtask(sid, st, preview=out, elapsed=time.time() - t0,
                       usage=(meta or {}).get("usage"),
                       model=(meta or {}).get("model"))
        if actual != name:
            status.state["subtasks"][sid]["failover"] = actual
            status.save()
        # keep whatever the delegate produced for the builder to reuse
        try:
            prior = proj_dir / "prior_work" / sid
            prior.mkdir(parents=True, exist_ok=True)
            for f in workdir.iterdir():
                if f.is_file() and f.name != "outbox.jsonl" \
                        and not f.name.endswith(".stream.log"):
                    shutil.copy2(f, prior / f.name)
        except OSError:
            pass
        return sid, out

    # 2. build-test-fix cycles ---------------------------------------------
    for cycle in range(1, max_cycles + 1):
        if cancelled():
            status.set_phase("stopped", "stopped by user")
            return run_dir
        status.state["cycle"] = cycle
        status.save()

        # (a) delegated side work in parallel
        todos = [s for s in (p.get("delegate") or [])
                 if isinstance(s, dict) and s.get("id")]
        if todos:
            status.set_phase("executing")
            pool = ThreadPoolExecutor(max_workers=min(len(todos), 4))
            futs = {pool.submit(delegate, s, cycle): s for s in todos}
            cap = max(int((agents.get(s["agent"]) or {}).get("timeout_sec", 900))
                      for s in todos) + 120
            done_f, strays = wait(futs, timeout=cap)
            pool.shutdown(wait=False, cancel_futures=True)
            for f in done_f:
                f.result()
            for f in strays:
                s = futs[f]
                sid = str(s.get("id"))
                status.subtask(sid, "failed",
                               preview=f"SUBTASK FAILED: time limit ({cap}s)")
            _maybe_wait_for_approval(mailbox, run_dir, status, cancel)

        # (b) build — the lead (builder) works in the project folder
        status.set_phase("building")
        bid = f"build-c{cycle}"
        status.add_subtask(bid, f"Build cycle {cycle}", builder)
        status.subtask(bid, "running",
                       started=datetime.now().isoformat(timespec="seconds"))
        state_bits = []
        if test_report.get("tests_run"):
            state_bits.append(
                "Test report from the previous cycle:\n"
                + json.dumps(test_report, ensure_ascii=False, indent=1))
        if build_notes:
            state_bits.append("Your build notes so far:\n" + build_notes)
        state_block = "\n\n".join(state_bits)
        if state_block:
            state_block += "\n\n"
        print(f"[harness] project: build cycle {cycle} by {builder}")
        t0 = time.time()
        try:
            build_out = invoke_agent(
                builder, agents.get(builder) or reg["agents"][builder],
                PROJECT_BUILD_PROMPT.format(
                    task=task_full, architecture=architecture,
                    state_block=state_block, prior_results="",
                    mailbox_block=""),
                proj_dir,
                stream_file=run_dir / "agents" / f"{bid}.stream.log",
                cancel_event=cancel)
            record_usage(status, bid, take_call_meta(), agent=builder)
            bst = "done"
        except AgentCancelled:
            raise
        except Exception as e:
            build_out, bst = f"BUILD FAILED: {e}", "failed"
        build_notes = build_out[-3000:]
        status.subtask(bid, bst, preview=build_out, elapsed=time.time() - t0)
        if cancelled():
            status.set_phase("stopped", "stopped by user")
            return run_dir

        # (c) test — actually run what was built
        status.set_phase("testing")
        tid = f"test-c{cycle}"
        status.add_subtask(tid, f"Test cycle {cycle}", builder)
        status.subtask(tid, "running",
                       started=datetime.now().isoformat(timespec="seconds"))
        print(f"[harness] project: test cycle {cycle}")
        t0 = time.time()
        try:
            test_raw = invoke_agent(
                builder, agents.get(builder) or reg["agents"][builder],
                PROJECT_TEST_PROMPT.format(task=task_full),
                proj_dir,
                stream_file=run_dir / "agents" / f"{tid}.stream.log",
                cancel_event=cancel)
            record_usage(status, tid, take_call_meta(), agent=builder)
            tst = "done"
        except AgentCancelled:
            raise
        except Exception as e:
            test_raw, tst = f"TEST STEP FAILED: {e}", "failed"
        try:
            test_report = extract_json(test_raw)
        except AgentError:
            test_report = {"tests_run": "?", "bugs": [test_raw[:500]],
                           "status": "fail"}
        status.subtask(tid, tst, preview=test_raw, elapsed=time.time() - t0)
        cycle_log.append(
            f"--- cycle {cycle} ---\ntests: {test_report.get('tests_run')}\n"
            f"bugs: {json.dumps(test_report.get('bugs') or [], ensure_ascii=False)}\n"
            f"status: {test_report.get('status')}")
        if cancelled():
            status.set_phase("stopped", "stopped by user")
            return run_dir

        # (d) lead decides: done or another cycle
        _budget_check(status, settings, mailbox, run_dir, cancel)
        status.set_phase("planning")
        loop_raw = invoke_agent(
            orch, agents[orch] if orch in agents else reg["agents"][orch],
            PROJECT_LOOP_PROMPT.format(
                task=task_full, cycle=cycle, max_cycles=max_cycles,
                test_report=json.dumps(test_report, ensure_ascii=False)[:2000],
                build_notes=build_notes[-2000:]),
            orch_dir,
            stream_file=orch_dir / f"loop-c{cycle}.stream.log",
            cancel_event=cancel)
        record_usage(status, f"loop-c{cycle}", take_call_meta(), agent=orch)
        try:
            p = extract_json(loop_raw)
        except AgentError:
            p = {"status": "continue" if cycle < max_cycles else "done",
                 "summary": "", "delegate": []}
        cycle_log.append(f"lead: {p.get('summary', '')}")
        print(f"[harness] project: cycle {cycle} -> {p.get('status')}")
        if p.get("status") == "done" or cycle == max_cycles:
            break

    # 3. final report ---------------------------------------------------------
    status.set_phase("synthesizing")
    # the lead wrote the code, but if it is rate-limited here the whole build
    # would report as failed — one failover to another strong agent, which
    # still gets the architecture and cycle log for context
    reporters = [orch]
    ralt = pick_review_agent(orch, agents, settings)
    if ralt:
        reporters.append(ralt[0])
    final = None
    synth_err: Exception | None = None
    for attempt, sname in enumerate(reporters):
        if cancelled():
            raise AgentCancelled("stopped by user")
        scfg = agents[sname] if sname in agents else reg["agents"][sname]
        try:
            final = invoke_agent(
                sname, scfg,
                PROJECT_SYNTH_PROMPT.format(task=task_full,
                                            architecture=architecture,
                                            cycle_log="\n\n".join(cycle_log),
                                            project_dir=str(proj_dir)),
                orch_dir,
                stream_file=orch_dir / ("synth.stream.log" if attempt == 0
                                        else "synth2.stream.log"),
                cancel_event=cancel)
            record_usage(status, "synth", take_call_meta(), agent=sname)
            if sname != orch:
                print(f"[harness] report failover: {sname} wrote the report")
                status.state["synthesizer"] = sname
            break
        except AgentCancelled:
            raise
        except Exception as e:
            synth_err = e
            print(f"[harness] report with {sname} failed: {e}")
    if final is None:
        raise AgentError(f"final report failed: {synth_err}")
    (run_dir / "result.md").write_text(final, encoding="utf-8")
    status.state["project_dir"] = "project"

    final = _final_review(task_full, final, orch, agents, reg, orch_dir, run_dir,
                          status, settings, cancel)
    _verify_project(task_full, final, orch, agents, reg, run_dir, proj_dir,
                    test_report, status, settings, cancel)
    _memory_wrapup(task_full, final, orch, agents, reg, orch_dir, run_dir,
                   status, settings, cancel)
    if cancelled():
        status.set_phase("stopped", "stopped by user")
        return run_dir
    status.state["result_file"] = "result.md"
    status.set_phase("done")
    print(f"[harness] project done -> {proj_dir}")
    return run_dir


# ---------------------------------------------------------------- cli

STATUS_MARKS = {"ready": "OK", "needs-login": "NEEDS LOGIN",
                "needs-key": "NEEDS KEY", "degraded": "DEGRADED",
                "offline": "OFFLINE", "not-installed": "NOT INSTALLED"}


def cmd_agents(_args):
    reg = load_registry()
    for name, cfg in reg["agents"].items():
        mark = STATUS_MARKS.get(cfg.get("status", ""), cfg.get("status", "?"))
        kind = cfg.get("type", "cli")
        print(f"{name:16} [{mark:12}] ({kind:3}) {cfg.get('description','')}")
    print(f"\norchestrator: {reg.get('orchestrator')}")


def cmd_doctor(_args):
    reg = load_registry()
    tmp = RUNS_DIR / "doctor"
    for name, cfg in reg["agents"].items():
        try:
            out = invoke_agent(name, cfg, "Reply with exactly: OK", tmp / name, reg)
            ok = "OK" in out
            print(f"{name:16} {'PASS' if ok else 'UNEXPECTED OUTPUT'}  {out[:120]!r}")
        except Exception as e:
            print(f"{name:16} FAIL  {e}")


def cmd_show(args):
    p = RUNS_DIR / args.run_id / "result.md"
    print(p.read_text(encoding="utf-8") if p.exists() else f"no result.md in {p.parent}")


FREE_HINTS = ("free",)  # openrouter marks free models with ':free'


def cmd_discover(args):
    """List (or add) currently free models from providers with a models_url."""
    reg = load_registry()
    providers = reg.get("_providers", {})
    targets = [args.provider] if args.provider else [
        p for p, c in providers.items() if c.get("models_url")]
    for pname in targets:
        pcfg = providers.get(pname)
        if not pcfg or not pcfg.get("models_url"):
            print(f"[{pname}] no models_url — skipped")
            continue
        key = _api_key(pname, reg)
        req = urllib.request.Request(
            pcfg["models_url"],
            headers={"Authorization": f"Bearer {key or 'none'}",
                     "User-Agent": "agent-harness/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[{pname}] fetch failed: {e}")
            continue
        models = [m.get("id", "") for m in data.get("data", [])]
        free = [m for m in models if any(h in m.lower() for h in FREE_HINTS)]
        show = free or models  # providers without :free tagging: show all
        print(f"\n[{pname}] {len(show)} models ({len(free)} tagged free):")
        for m in show:
            print(f"  {m}")
        if args.add and show:
            added = _append_discovered(pname, show[: args.add])
            print(f"  -> added {added} agent(s) to agents.toml")


def _append_discovered(provider: str, models: list[str]) -> int:
    """Append simple api-agent blocks to agents.toml for discovered models."""
    existing = REGISTRY_PATH.read_text(encoding="utf-8")
    count = 0
    with open(REGISTRY_PATH, "a", encoding="utf-8") as f:
        for model in models:
            safe = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:40]
            name = f"{provider}-{safe}"
            if f"[agents.{name}]" in existing:
                continue
            f.write(f"""
[agents.{name}]
type = "api"
provider = "{provider}"
models = ["{model}"]
description = "{model} (discovered, free tier)."
strengths = ["general"]
status = "needs-key"
timeout_sec = 180
""")
            count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description="Multi-agent harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run a task through the agent team")
    p_run.add_argument("task")
    p_run.add_argument("--orchestrator")
    p_run.add_argument("--agents", help="comma-separated worker pool (default: all)")
    p_run.add_argument("--pattern", choices=["standard", "council", "best-of-n",
                                             "project"],
                       default="standard",
                       help="standard = planner splits work; council/best-of-n = "
                            "everyone answers, the orchestrator judges; project = "
                            "lead agent builds & tests real files in cycles")

    sub.add_parser("agents", help="list configured agents")
    sub.add_parser("doctor", help="health-check every agent headlessly")

    p_disc = sub.add_parser("discover", help="list free models from providers")
    p_disc.add_argument("provider", nargs="?", help="only this provider")
    p_disc.add_argument("--add", type=int, metavar="N",
                        help="append up to N discovered models to agents.toml")

    p_show = sub.add_parser("show", help="print a run's final result")
    p_show.add_argument("run_id")

    args = ap.parse_args()
    if args.cmd == "run":
        filt = args.agents.split(",") if args.agents else None
        run_task(args.task, args.orchestrator, filt, pattern=args.pattern)
    elif args.cmd == "agents":
        cmd_agents(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)
    elif args.cmd == "discover":
        cmd_discover(args)
    elif args.cmd == "show":
        cmd_show(args)


if __name__ == "__main__":
    main()
