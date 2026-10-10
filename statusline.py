# /// script
# requires-python = ">=3.10"
# ///

"""Unified status line for Claude Code.

Sections (left to right):
    model · effort │ ctx bar N% used/size │ 5h bar N% reset │ 7d bar N% reset │ $cost │ duration │ project › git │ venv │ :ports

Every section after model + ctx is auto-detected and shown only when relevant.
Standalone: no external dependencies.

Usage numbers: Claude Code only pipes in the rate limits from *this* session's
last reply, so they lag whenever other sessions (or claude.ai) use the shared
quota. The script also reads the endpoint behind /usage in a background process,
caches it for USAGE_TTL seconds, and shows the higher of the two within a window.
Set STATUSLINE_LIVE_USAGE=0 to turn that off.

Install:
    1. Drop this file at ~/.claude/statusline.py
    2. Add to ~/.claude/settings.json:
           "statusLine": {
               "type": "command",
               "command": "python ~/.claude/statusline.py",
               "refreshInterval": 30
           }
       (Swap "python" for "uv run" if you use uv.)
    3. Restart your Claude Code session.

See https://github.com/roeimichael/claude-code-statusline for docs.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# ANSI colors (256-color palette, so macOS Terminal.app renders them too)
def fg(n):
    return f"\033[38;5;{n}m"


MODEL = fg(173)    # Claude orange
PROJECT = fg(141)  # violet
GREEN = fg(114)
AMBER = fg(221)
CYAN = fg(80)
DIM = "\033[2m"
RST = "\033[0m"
# green → amber → red, indexed by percentage
RAMP = [fg(n) for n in (78, 114, 150, 186, 221, 215, 209, 203)]

SEP = f" {DIM}│{RST} "
BAR_ON, BAR_OFF = "▰", "▱"
EFFORT = {"low": DIM, "medium": GREEN, "high": AMBER, "xhigh": fg(209), "max": fg(203)}

# Second line: Claude Code version, plugin reload and MCP server status.
SHOW_HEALTH = True

# Edit this list to match the dev ports you care about.
COMMON_PORTS = [3000, 3001, 5173, 5174, 8000, 8080, 8081, 8086, 9086, 9087, 54321]

# Live usage (the data /usage shows). Seconds between background fetches.
USAGE_TTL = 60
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
USAGE_CACHE = CLAUDE_DIR / "statusline-usage.json"
USAGE_LOCK = CLAUDE_DIR / "statusline-usage.lock"
WINDOWS = ("five_hour", "seven_day")

# Release check: newest Claude Code on your update channel. Seconds between fetches.
RELEASE_TTL = 3600
RELEASE_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
NATIVE_VERSIONS = Path.home() / ".local" / "share" / "claude" / "versions"

OK, UP, WARN = "\u2713", "\u2191", "\u25b2"


def color_pct(p):
    i = int(max(0, min(99.9, p)) / 100 * len(RAMP))
    return RAMP[i]


def bar(p, width=8):
    filled = max(0, min(width, round(p / 100 * width)))
    return f"{color_pct(p)}{BAR_ON * filled}{RST}{DIM}{BAR_OFF * (width - filled)}{RST}"


def meter(label, p, suffix=""):
    """'label ▰▰▱▱ N% suffix', or 'label --' when the value is unknown."""
    if p is None:
        return f"{DIM}{label} --{RST}"
    tail = f" {DIM}{suffix}{RST}" if suffix else ""
    return f"{DIM}{label}{RST} {bar(p)} {color_pct(p)}{p:.0f}%{RST}{tail}"


def fmt_duration(ms):
    if not ms or ms <= 0:
        return ""
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def fmt_until(seconds):
    """Compact countdown: 14m, 3h05m, 5d2h."""
    if seconds is None or seconds <= 0:
        return ""
    m = int(seconds // 60)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{max(m, 1)}m"


def fmt_tokens(n):
    """Compact token count: 950, 12k, 135k, 1M, 1.2M."""
    if n < 1000:
        return str(n)
    if n < 999_500:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def context_tokens(cw):
    """'used/size' tokens in the context window, or '' when the size is unknown."""
    size = cw.get("context_window_size")
    if not size:
        return ""
    u = cw.get("current_usage") or {}
    used = sum(int(u.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return f"{fmt_tokens(used)}/{fmt_tokens(int(size))}"


def fmt_cost(c):
    return f"${c:.1f}" if c >= 10 else f"${c:.2f}"


def run(cmd, timeout=1.0):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# --- usage -----------------------------------------------------------------

def _epoch(v):
    """resets_at arrives as epoch seconds (stdin) or ISO 8601 (usage endpoint)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(pct, resets_at, now):
    if pct is None:
        return None
    reset = _epoch(resets_at)
    if reset is not None and reset <= now:
        # The window rolled over and nothing newer says otherwise: it is empty.
        return {"pct": 0.0, "resets_at": None}
    return {"pct": float(pct), "resets_at": reset}


def merge_usage(stdin_rl, live, now):
    """Combine this session's rate limits with the account-wide live reading.

    Usage only grows within a window, so for the same window the higher number
    is the fresher one. A window that resets later is a newer window and wins.
    Returns {window: {"pct", "resets_at"} | None}; None means unknown.
    """
    stdin_rl = stdin_rl or {}
    live = live or {}
    out = {}
    for key in WINDOWS:
        s = stdin_rl.get(key) or {}
        l = live.get(key) or {}
        cands = [
            w for w in (
                _window(s.get("used_percentage"), s.get("resets_at"), now),
                _window(l.get("utilization"), l.get("resets_at"), now),
            ) if w
        ]
        if not cands:
            out[key] = None
            continue
        # Same window when resets_at agree to within a minute (the sources round differently).
        latest = max((w["resets_at"] or 0) for w in cands)
        same = [w for w in cands if (w["resets_at"] or 0) >= latest - 60]
        out[key] = max(same, key=lambda w: w["pct"])
    return out


def read_oauth_token():
    """The Claude subscription token, or None (API-key users, expired, unreadable)."""
    raw = None
    try:
        raw = (CLAUDE_DIR / ".credentials.json").read_text(encoding="utf-8")
    except OSError:
        if sys.platform == "darwin":
            raw = run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], 2.0)
    try:
        oauth = json.loads(raw)["claudeAiOauth"]
    except Exception:
        return None
    expires = oauth.get("expiresAt")
    if expires and expires / 1000 <= time.time():
        return None  # Claude Code refreshes it on its next request.
    return oauth.get("accessToken")


def load_usage_cache():
    try:
        return json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_cache():
    """Fetch whatever is stale into the cache. Runs in a detached child process."""
    cache = load_usage_cache()
    now = time.time()
    if now - (cache.get("fetched_at") or 0) >= USAGE_TTL:
        _fetch_usage(cache, now)
    if now - (cache.get("tags_at") or 0) >= RELEASE_TTL:
        cache["tags_at"] = now
        try:
            with urllib.request.urlopen(RELEASE_URL, timeout=5) as r:
                cache["tags"] = json.loads(r.read())
        except Exception:
            pass
    tmp = USAGE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, USAGE_CACHE)


def _fetch_usage(cache, now):
    cache["fetched_at"] = now
    try:
        token = read_oauth_token()
        if not token:
            raise RuntimeError("no oauth token")
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        cache["usage"] = {k: body.get(k) for k in WINDOWS}
        cache["usage_at"] = cache["fetched_at"]
        cache["error"] = None
    except Exception as e:
        cache["error"] = f"{type(e).__name__}: {e}"[:200]


def _take_lock(now):
    for _ in range(2):
        try:
            os.close(os.open(USAGE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if now - USAGE_LOCK.stat().st_mtime < 30:
                    return False  # a refresh is already running
                USAGE_LOCK.unlink()  # left behind by a killed refresh
            except OSError:
                return False
        except OSError:
            return False
    return False


def maybe_refresh(cache, now):
    """Start a background fetch when anything in the cache is stale. Never blocks."""
    stale = (now - (cache.get("fetched_at") or 0) >= USAGE_TTL
             or now - (cache.get("tags_at") or 0) >= RELEASE_TTL)
    if not stale or not _take_lock(now):
        return
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh"], **kw)
    except Exception:
        USAGE_LOCK.unlink(missing_ok=True)


# --- health: version, plugins, MCP ------------------------------------------

def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split("-")[0].split("."))
    except ValueError:
        return ()


def version_status(running, installed, published):
    """('ok'|'restart'|'update'|'unknown', newer version or None)."""
    newest_installed = max(installed, key=_ver, default=None)
    if newest_installed and _ver(newest_installed) > _ver(running):
        return "restart", newest_installed
    if published and _ver(published) > _ver(running):
        return "update", published
    if published or installed:
        return "ok", None
    return "unknown", None


def installed_versions():
    try:
        return [d.name for d in NATIVE_VERSIONS.iterdir() if _ver(d.name)]
    except OSError:
        return []


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def plugins_needing_reload(installed, enabled, loaded_at):
    """Enabled plugins updated on disk after this session loaded its plugins."""
    out = []
    for name, entries in (installed.get("plugins") or {}).items():
        if name not in enabled:
            continue
        updated = max((_epoch(e.get("lastUpdated")) or 0 for e in entries), default=0)
        if updated > loaded_at:
            out.append(name)
    return sorted(out)


def plugins_loaded_at(transcript_path):
    """When this session last loaded plugins: its first entry, or the last /reload-plugins."""
    try:
        raw = Path(transcript_path).read_bytes()
    except (OSError, TypeError):
        return None

    def ts_of(line):
        try:
            return _epoch(json.loads(line).get("timestamp"))
        except ValueError:
            return None

    i = raw.rfind(b"<command-name>/reload-plugins</command-name>")
    if i >= 0:
        start = raw.rfind(b"\n", 0, i) + 1
        end = raw.find(b"\n", i)
        t = ts_of(raw[start:end if end >= 0 else None])
        if t:
            return t
    for line in raw.split(b"\n", 50)[:50]:
        t = ts_of(line) if line.strip() else None
        if t:
            return t
    return None


def mcp_status(logs, session_id, known_plugins=()):
    """(connected, failed) server names from this session's MCP log events.

    logs: {log dir suffix: [event dicts]}. Only connection events count, so a
    tool call that errors on a connected server is not a server problem.
    """
    ok, failed = [], []
    for server, events in logs.items():
        state = None
        for e in sorted((e for e in events if e.get("sessionId") == session_id),
                        key=lambda e: e.get("timestamp", "")):
            if "Successfully connected" in str(e.get("debug", "")):
                state = True
            elif str(e.get("error", "")).startswith("Connection failed"):
                state = False
        if state is None:
            continue
        (ok if state else failed).append(_mcp_name(server, known_plugins))
    return sorted(ok), sorted(failed)


def _mcp_name(server, known_plugins=()):
    """Log dir suffix to display name: plugin-discord-discord -> discord, claude-ai-Gmail -> Gmail.

    Plugin dirs are plugin-<plugin>-<server>, and both halves may contain hyphens,
    so split where the plugin half is a known plugin or the halves repeat.
    """
    if server.startswith("claude-ai-"):
        return server[10:]
    if not server.startswith("plugin-"):
        return server
    words = server[7:].split("-")
    for i in range(1, len(words)):
        plugin, name = "-".join(words[:i]), "-".join(words[i:])
        if plugin == name or plugin in known_plugins:
            return name
    return server[7:]


def mcp_log_root(project_dir):
    """Claude Code's per-project cache dir, where it writes mcp-logs-<server>/."""
    if not project_dir:
        return None
    key = "".join(c if c.isalnum() else "-" for c in str(project_dir))
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "claude-cli-nodejs" / "Cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "claude-cli-nodejs"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "claude-cli-nodejs"
    return base / key


def read_mcp_logs(root, files_per_server=3):
    """{server: [events]} from the newest log files, or None when there are no logs."""
    try:
        dirs = [d for d in root.iterdir() if d.name.startswith("mcp-logs-")]
    except (OSError, AttributeError):
        return None
    logs = {}
    for d in dirs:
        events = []
        for f in sorted(d.glob("*.jsonl"), reverse=True)[:files_per_server]:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        logs[d.name[9:]] = events
    return logs


def format_health(running, version, reload_plugins, mcp):
    """Second line: Claude Code version, plugin reload, MCP servers. '' when nothing is known."""
    parts = []
    if running:
        state, newer = version
        if state == "ok":
            parts.append(f"{DIM}cc{RST} {running} {GREEN}{OK}{RST}")
        elif state in ("update", "restart"):
            tail = f" {DIM}restart{RST}" if state == "restart" else ""
            parts.append(f"{DIM}cc{RST} {running} {AMBER}{UP} {newer}{RST}{tail}")
        else:
            parts.append(f"{DIM}cc{RST} {running}")
    if reload_plugins is not None:
        if reload_plugins:
            parts.append(f"{DIM}plugins{RST} {AMBER}{UP} {len(reload_plugins)}{RST} {DIM}/reload-plugins{RST}")
        else:
            parts.append(f"{DIM}plugins{RST} {GREEN}{OK}{RST}")
    if mcp is not None:
        ok, failed = mcp
        if ok or failed:
            s = f"{DIM}mcp{RST} {len(ok)}"
            if failed:
                more = f" +{len(failed) - 1}" if len(failed) > 1 else ""
                s += f" {RAMP[-1]}{WARN} {failed[0]}{more}{RST}"
            parts.append(s)
    return SEP.join(parts)


def health_line(data):
    settings = _read_json(CLAUDE_DIR / "settings.json")
    running = data.get("version")
    channel = settings.get("autoUpdatesChannel") or "latest"
    published = (load_usage_cache().get("tags") or {}).get(channel)
    version = version_status(running, installed_versions(), published) if running else ("unknown", None)

    reload_plugins = None
    loaded_at = plugins_loaded_at(data.get("transcript_path"))
    installed = _read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json")
    if loaded_at and installed:
        enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}
        reload_plugins = plugins_needing_reload(installed, enabled, loaded_at)

    ws = data.get("workspace") or {}
    logs = read_mcp_logs(mcp_log_root(ws.get("project_dir") or data.get("cwd")))
    known = {k.split("@")[0] for k in (installed.get("plugins") or {})}
    mcp = mcp_status(logs, data.get("session_id"), known) if logs else None
    return format_health(running, version, reload_plugins, mcp)


# --- workspace probes --------------------------------------------------------

def git_info(cwd):
    if not cwd or not Path(cwd).exists():
        return ""
    # One git call instead of five: branch plus staged/modified/untracked counts.
    out = run(["git", "-C", cwd, "status", "--porcelain=v2", "--branch", "--untracked-files=all"])
    branch = oid = ""
    stg = mod = unt = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[14:]
        elif line.startswith("# branch.oid "):
            oid = line[13:20]
        elif line[:2] in ("1 ", "2 "):
            stg += line[2] != "."
            mod += line[3] != "."
        elif line.startswith("u "):
            mod += 1
        elif line.startswith("? "):
            unt += 1
    if branch == "(detached)":
        branch = oid
    if not branch:
        return ""
    if len(branch) > 22:
        branch = branch[:20] + ".."
    flags = []
    if stg:
        flags.append(f"{GREEN}+{stg}{RST}")
    if mod:
        flags.append(f"{AMBER}~{mod}{RST}")
    if unt:
        flags.append(f"{DIM}?{unt}{RST}")
    color = AMBER if flags else GREEN
    return " ".join([f"{color}{branch}{RST}"] + flags)


def venv_tag():
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda and conda != "base":
        return f"{DIM}env:{RST}{CYAN}{conda}{RST}"
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return f"{DIM}venv:{RST}{CYAN}{Path(venv).name}{RST}"
    return ""


def port_up(port, timeout=0.05):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return False


def ports_tag():
    # In parallel: on Windows every closed port waits out the full timeout.
    with ThreadPoolExecutor(max_workers=len(COMMON_PORTS) or 1) as pool:
        up = [p for p, ok in zip(COMMON_PORTS, pool.map(port_up, COMMON_PORTS)) if ok]
    return f"{GREEN}:{','.join(str(p) for p in up)}{RST}" if up else ""


# --- layout ------------------------------------------------------------------

def format_statusline(data, usage=None, now=None, show_usage=True):
    """usage: merge_usage() result, or None when nothing is known yet."""
    now = time.time() if now is None else now
    cw = data.get("context_window", {}) or {}
    cost = data.get("cost", {}) or {}
    model = data.get("model", {}) or {}
    ws = data.get("workspace", {}) or {}

    name = model.get("display_name", "Claude")
    head = f"{MODEL}{name}{RST}"
    level = (data.get("effort") or {}).get("level")
    if level:
        head += f" {DIM}·{RST} {EFFORT.get(level, '')}{level}{RST}"
    parts = [head]
    parts.append(meter("ctx", float(cw.get("used_percentage", 0) or 0), context_tokens(cw)))

    if show_usage:
        usage = usage or {}
        for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
            w = usage.get(key)
            if w is None:
                parts.append(meter(label, None))
            else:
                left = w["resets_at"] - now if w["resets_at"] else None
                parts.append(meter(label, w["pct"], fmt_until(left)))

    parts.append(fmt_cost(float(cost.get("total_cost_usd", 0.0) or 0.0)))
    dur = fmt_duration(int(cost.get("total_duration_ms", 0) or 0))
    if dur:
        parts.append(f"{DIM}{dur}{RST}")

    cwd = ws.get("current_dir") or ws.get("project_dir") or data.get("cwd") or os.getcwd()
    proj = Path(cwd).name if cwd else ""
    git_s = git_info(cwd)
    where = [s for s in (f"{PROJECT}{proj}{RST}" if proj else "", git_s) if s]
    if where:
        parts.append(f" {DIM}›{RST} ".join(where))
    for extra in (venv_tag(), ports_tag()):
        if extra:
            parts.append(extra)
    return SEP.join(parts)


def main():
    if "--refresh" in sys.argv:
        try:
            refresh_cache()
        finally:
            USAGE_LOCK.unlink(missing_ok=True)
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        now = time.time()
        stdin_rl = data.get("rate_limits") or {}
        live = None
        show_usage = bool(stdin_rl)
        if os.environ.get("STATUSLINE_LIVE_USAGE", "1") != "0":
            cache = load_usage_cache()
            live = cache.get("usage")
            if read_oauth_token() or live:
                show_usage = True
                maybe_refresh(cache, now)
        usage = merge_usage(stdin_rl, live, now)
        known = any(usage.values())
        line = format_statusline(data, usage if known else None, now, show_usage)
        if SHOW_HEALTH:
            try:
                health = health_line(data)
                if health:
                    line += "\n" + health
            except Exception:
                pass
        print(line)
    except Exception:
        print("Claude | --")
    sys.exit(0)


if __name__ == "__main__":
    main()
