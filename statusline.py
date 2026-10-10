# /// script
# requires-python = ">=3.10"
# ///

"""Unified status line for Claude Code.

One line, always (Claude Code draws its own mode line below it), left to right:
    project › branch ~N “session-name” │ model · effort │ ctx N% bar used/size │
    5h N% bar reset │ 7d N% bar reset │ $cost │ session time │ health marks │
    skills │ venv │ :ports

It is designed for a 120-column window and fits itself to COLUMNS: wider windows show
more, narrower ones drop items by priority (see format_statusline). It never wraps.
Items with nothing to show (no session name, no ports, no venv) are left out.
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
               "refreshInterval": 10
           }
       (Swap "python" for "uv run" if you use uv.)
    3. Restart your Claude Code session.

See https://github.com/roeimichael/claude-code-statusline for docs.
"""

import json
import os
import re
import sys
import time
import zlib
from pathlib import Path

# subprocess, socket and datetime are imported where they are used: a render with a
# warm probe cache needs none of them, and it runs every second.

# ANSI colors (256-color palette, so macOS Terminal.app renders them too)
def fg(n):
    return f"\033[38;5;{n}m"


MODEL = fg(173)    # Claude orange
PROJECT = fg(141)  # violet
GREEN = fg(114)
AMBER = fg(221)
CYAN = fg(80)
COST = "\033[1;38;5;46m"  # bold bright green
# One color per model family, used for the model name and for subagents on that model.
MODEL_COLORS = {"opus": fg(208), "sonnet": fg(75), "haiku": fg(43), "fable": fg(213)}


def model_color(model_id):
    m = str(model_id or "").lower()
    for family, color in MODEL_COLORS.items():
        if family in m:
            return color
    return MODEL
DIM = fg(252)  # secondary text: the terminal's usual light grey, not a faint dim
RST = "\033[0m"
RED = fg(203)
# Meter colors by percentage: (below this %, color), then RED.
STAGES = ((50, fg(114)), (70, fg(221)), (85, fg(208)))

SEP = f" {DIM}│{RST} "  # one space each side: room for three bars on one 120-column line
# Columns kept free at the window's right edge (Claude Code pads the status line).
EDGE = 4
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_width(s):
    return len(ANSI_RE.sub("", s))


def mid_trunc(s, n):
    """Shorten s to n characters by replacing its middle with '…'."""
    if len(s) <= n:
        return s
    if n <= 1:
        return "…"[:n]
    keep = n - 1
    head = keep // 2
    return s[:head] + "…" + s[len(s) - (keep - head):]
BAR = "\u2588"          # full block: fills the whole cell height
BAR_TRACK = fg(238)      # the empty part of a bar
EFFORT = {"low": DIM, "medium": GREEN, "high": AMBER, "xhigh": fg(209), "max": fg(203)}

# Health marks: MCP servers, Claude Code version and plugin reload state.
SHOW_HEALTH = True
# Extra line listing running subagents (model, effort, context tokens). Off by default:
# the agent list under the prompt is the better place, see the companion mod. It is the
# one way to get a second line.
SHOW_AGENTS = False

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

# Icons. "none" uses plain text; "nerd" needs a Nerd Font in the terminal
# (e.g. CaskaydiaMono Nerd Font). Pick with --icons nerd or STATUSLINE_ICONS=nerd.
# Plain mode also serves the classic Windows console (Consolas), so only glyphs it has.
PLAIN_ICONS = {"ok": "\u2713", "up": "\u2191", "warn": "\u25b2", "reload": "~"}
NERD_ICONS = {
    "model": "\U000F167A",    # md-robot_outline
    "ctx": "\U000F0830",      # md-poker_chip (tokens)
    "five_hour": "\U000F051F",  # md-timer_sand
    "seven_day": "\U000F0A33",  # md-calendar_week
    "cost": "\U000F01C1",     # md-currency_usd
    "duration": "\U000F0150",  # md-clock_outline
    "project": "\U000F024B",  # md-folder
    "branch": "\ue725",       # dev-git_branch
    "venv": "\U000F0320",     # md-language_python
    "ports": "\U000F0318",    # md-lan_connect
    "cc": "",           # cod-claude: the Claude logo
    "plugins": "\U000F0431",  # md-puzzle
    "skills": "\U000F140B",   # md-lightning_bolt
    "mcp": "\U000F048D",      # md-server_network
    "agents": "\U000F171A",   # md-robot_happy_outline
    "ok": "\U000F05E0",       # md-check_circle
    "up": "\U000F0CE1",       # md-arrow_up_circle
    "reload": "\U000F0453",   # md-reload
    "warn": "\U000F0026",     # md-alert
    "tag": "\U000F04FC",      # md-tag_outline: the session name
    "segment": "",  # powerline slants: one tilted full-height bar segment
}
ICON = dict(PLAIN_ICONS)


def console_window_class():
    """Window class of this process's console on Windows ('' when it has none), else None.

    The classic console host is 'ConsoleWindowClass'; Windows Terminal's ConPTY is
    'PseudoConsoleWindow'.
    """
    if os.name != "nt":
        return None
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return None  # a terminal that says what it is: no need to pay for ctypes (~10 ms)
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return None


def effective_icons(mode):
    """The requested icon set, unless this terminal cannot draw it.

    The classic Windows console shows Nerd Font glyphs as boxes (and miscounts their
    width), so 'nerd' falls back to 'none' there. A console without a window counts
    too, unless Windows Terminal (WT_SESSION) or another terminal (TERM_PROGRAM) set
    its mark. STATUSLINE_FORCE_ICONS=1 skips the check. With WT_SESSION or TERM_PROGRAM
    set, console_window_class() does not even ask Windows (the answer is 'keep nerd').
    """
    if mode != "nerd" or os.environ.get("STATUSLINE_FORCE_ICONS"):
        return mode
    cls = console_window_class()
    if cls == "ConsoleWindowClass":
        return "none"
    if cls == "" and not os.environ.get("WT_SESSION") and not os.environ.get("TERM_PROGRAM"):
        return "none"
    return mode


def set_icons(mode):
    ICON.clear()
    ICON.update(NERD_ICONS if mode == "nerd" else PLAIN_ICONS)


def ic(name, color=DIM):
    """Icon followed by a space, or '' when the current icon set has none."""
    g = ICON.get(name)
    return f"{color}{g}{RST} " if g else ""


def color_pct(p):
    """Four clear stages: green, yellow from 50%, orange from 70%, red from 85%."""
    for limit, color in STAGES:
        if p < limit:
            return color
    return RED


def bar(p, width=5, segments=3):
    """A bar of `width` full blocks, or `segments` slanted segments with Nerd icons."""
    if ICON.get("segment"):
        seg, width = ICON["segment"], segments
    else:
        seg = BAR
    # Any usage lights the first segment, so a low reading never looks empty.
    filled = max(1 if p > 0 else 0, min(width, round(p / 100 * width)))
    return f"{color_pct(p)}{seg * filled}{RST}{BAR_TRACK}{seg * (width - filled)}{RST}"


def meter(label, p, icon, with_bar=True, suffix=""):
    """'ctx 43% ▰▰▱ 430k/1M': label and value in the stage color, then bar and a light-grey suffix.

    'ctx --' (no bar, no suffix) when the reading is unknown.
    """
    if p is None:
        return f"{ic(icon)}{DIM}{label} --{RST}"
    c = color_pct(p)
    out = f"{ic(icon, c)}{c}{label}{RST} {c}{p:.0f}%{RST}"
    if with_bar:
        out += f" {bar(p)}"
    if suffix:
        out += f" {DIM}{suffix}{RST}"
    return out


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
    """Compact countdown: 14m, 3h, 3h05m, 5d, 5d2h."""
    if seconds is None or seconds <= 0:
        return ""
    m = int(seconds // 60)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d{h}h" if h else f"{d}d"
    if h:
        return f"{h}h{m:02d}m" if m else f"{h}h"
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
    u = cw.get("current_usage")
    if not size or not isinstance(u, dict):
        return ""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if all(u.get(k) is None for k in keys):
        return ""  # no reply yet: unknown, not "0 tokens"
    used = sum(int(u.get(k) or 0) for k in keys)
    return f"{fmt_tokens(used)}/{fmt_tokens(int(size))}"


def context_pct(cw):
    """Context window usage in percent, or None when Claude Code has not said."""
    try:
        return float(cw.get("used_percentage"))
    except (TypeError, ValueError):
        return None


def fmt_cost(c):
    amount = f"{c:.2f}"
    if ICON.get("cost"):
        return f"{ic('cost', COST)}{COST}{amount}{RST}"
    return f"{COST}${amount}{RST}"


def run(cmd, timeout=1.0):
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# --- probe cache -------------------------------------------------------------
# With a 1-second refreshInterval every render must be cheap, so the slow probes
# (git, port scan, health) keep their results for a few seconds in one JSON file
# shared by all sessions. Only probe results go in, never the stdin payload's live
# numbers (percentages, cost, session time), so those are always current.

PROBE_CACHE_NAME = "statusline-probe-cache.json"
GIT_TTL = 3        # git status, per directory
PORTS_TTL = 10     # dev port scan
HEALTH_TTL = 10    # version status, plugins to reload, MCP status, per session and project
SKILLS_TTL = 60    # skill count (a folder scan), per project
NAME_REFRESH = 30  # the session name is keyed by transcript size, not a TTL; it is re-stored this often so it never expires


class ProbeCache:
    """{key: {"at": epoch, "v": value}} loaded once per render and saved at the end.

    Values come back as JSON, so tuples come back as lists. A missing, corrupt or
    unwritable file only means the probes run again; it never breaks rendering.
    """

    SKEW = 2      # seconds another session's clock reading may be ahead of ours
    CLAIM_TTL = 5  # seconds before a claim left by a dead session is ignored

    def __init__(self, path, now):
        self.path, self.now, self.dirty = path, now, False
        self.touched = set()  # keys this render computed: the only ones it writes back
        self.entries = self._read()

    def _read(self):
        if self.path is None:
            return {}
        try:
            entries = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except Exception:
            return {}
        return entries if isinstance(entries, dict) else {}

    @staticmethod
    def _at(e):
        """The entry's timestamp, or None when it is not a well-formed entry."""
        if isinstance(e, dict) and "v" in e and _number(e.get("at")):
            return e["at"]
        return None

    def _claim_path(self, key):
        return Path(f"{self.path}.{zlib.crc32(key.encode('utf-8')):08x}.claim")

    def _claim(self, key):
        """True when this session may recompute key now; False when another one already is."""
        claim = self._claim_path(key)
        for _ in range(2):
            try:
                os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - claim.stat().st_mtime < self.CLAIM_TTL:
                        return False
                    claim.unlink()  # a session died mid-probe
                except OSError:
                    return False
            except OSError:
                return True  # cannot coordinate: just probe
        return False

    def _release(self, key):
        try:
            self._claim_path(key).unlink()
        except OSError:
            pass

    def get(self, key, ttl, compute):
        """The cached value for key while younger than ttl seconds, else compute() (and keep it).

        When the entry has expired and another session is already recomputing it, the
        stale value is returned instead, so concurrent sessions do not all probe at once.
        """
        e = self.entries.get(key)
        at = self._at(e)
        stale = at is not None
        if stale and -self.SKEW <= self.now - at < ttl:
            return e["v"]
        claimed = False
        if stale and self.path is not None:
            if not self._claim(key):
                return e["v"]
            claimed = True
        try:
            value = compute()
        finally:
            if claimed:
                self._release(key)
        self.entries[key] = {"at": self.now, "v": value}
        self.touched.add(key)
        self.dirty = True
        return value

    def peek(self, key):
        """A copy of the dict stored under key (even an expired one), or {}."""
        e = self.entries.get(key)
        return dict(e["v"]) if self._at(e) is not None and isinstance(e["v"], dict) else {}

    def age(self, key):
        """Seconds since key was stored, or None when it is missing or malformed."""
        at = self._at(self.entries.get(key))
        return None if at is None else self.now - at

    def put(self, key, value):
        """Store value under key now, without any TTL check."""
        self.entries[key] = {"at": self.now, "v": value}
        self.touched.add(key)
        self.dirty = True

    def save(self):
        """Merge with what is on disk now, then write atomically (tmp file + os.replace).

        Other sessions save to the same file, so this render's computed entries are laid
        over a fresh read (the newest `at` per key wins) instead of over the copy loaded
        at the start. Entries no TTL can still use are dropped.
        """
        if self.path is None or not self.dirty:
            return
        keep = max(GIT_TTL, PORTS_TTL, HEALTH_TTL, SKILLS_TTL)
        tmp = Path(f"{self.path}.{os.getpid()}.tmp")
        lock = Path(f"{self.path}.lock")
        locked = self._lock(lock)  # without it (timeout, odd folder) the merge is just best effort
        try:
            merged = {k: e for k, e in self._read().items() if self._at(e) is not None}
            for key in self.touched:
                mine, theirs = self.entries[key], merged.get(key)
                if theirs is None or theirs["at"] <= mine["at"]:
                    merged[key] = mine
            entries = {k: e for k, e in merged.items() if -self.SKEW <= self.now - e["at"] < keep}
            tmp.write_text(json.dumps(entries), encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # a reader has the file open (Windows)
                    if attempt == 2:
                        raise
                    time.sleep(0.005)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
        finally:
            if locked:
                try:
                    lock.unlink()
                except OSError:
                    pass

    def _lock(self, lock):
        """Hold the save lock (a file made with O_EXCL), waiting up to ~50 ms; False if not got."""
        deadline = time.monotonic() + 0.05
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 2:
                        lock.unlink()  # left by a killed session
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.002)
            except OSError:
                return False


def _probe_cache(cache):
    """The render's ProbeCache, or a throwaway one (always probes) when none is given."""
    return cache if cache is not None else ProbeCache(None, time.time())


# --- usage -----------------------------------------------------------------

def _epoch(v):
    """resets_at arrives as epoch seconds (stdin) or ISO 8601 (usage endpoint)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(pct, resets_at, now):
    if pct is None:
        return None
    reset = _epoch(resets_at)
    if reset is not None and reset <= now:
        # The window rolled over; this reading says nothing about the new one.
        return None
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


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _clean_usage_cache(raw):
    """Only the well-shaped parts of a parsed usage cache; anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in ("fetched_at", "usage_at", "tags_at") if _number(raw.get(k))}
    if isinstance(raw.get("error"), str):
        out["error"] = raw["error"]
    tags = raw.get("tags")
    if isinstance(tags, dict):
        out["tags"] = {k: v for k, v in tags.items() if isinstance(k, str) and isinstance(v, str)}
    usage = raw.get("usage")
    if isinstance(usage, dict):
        clean = {}
        for key in WINDOWS:
            w = usage.get(key)
            if not isinstance(w, dict) or not _number(w.get("utilization")):
                continue
            resets = w.get("resets_at")
            if resets is not None and not (_number(resets) or (isinstance(resets, str) and _epoch(resets) is not None)):
                continue
            clean[key] = {"utilization": w["utilization"], "resets_at": resets}
        out["usage"] = clean
    return out


def load_usage_cache():
    """The usage cache as a dict of well-shaped values; {} when missing, corrupt or the wrong type."""
    try:
        return _clean_usage_cache(json.loads(USAGE_CACHE.read_text(encoding="utf-8")))
    except Exception:
        return {}


def refresh_cache():
    """Fetch whatever is stale into the cache. Runs in a detached child process."""
    import urllib.request  # here, not at the top: ~30ms the render itself never needs
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
    import urllib.request
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


def _take_lock(now, token=""):
    """Create the usage lock holding `token` (who owns it); False when a fresh one exists."""
    for _ in range(2):
        try:
            fd = os.open(USAGE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
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


def _release_lock(token):
    """Remove the usage lock, but only if it still holds our token (it may be someone else's by now)."""
    try:
        if token and USAGE_LOCK.read_text(encoding="utf-8") == token:
            USAGE_LOCK.unlink()
    except OSError:
        pass


REFRESH_DEADLINE = 15  # seconds the detached refresh may live, however slow the network is


def _expire(token):
    """The deadline passed: give up the lock (if ours) and leave at once."""
    _release_lock(token)
    os._exit(1)


def _arm_deadline(token):
    import threading
    timer = threading.Timer(REFRESH_DEADLINE, _expire, (token,))
    timer.daemon = True
    timer.start()


def maybe_refresh(cache, now):
    """Start a background fetch when anything in the cache is stale. Never blocks."""
    stale = (now - (cache.get("fetched_at") or 0) >= USAGE_TTL
             or now - (cache.get("tags_at") or 0) >= RELEASE_TTL)
    token = f"{os.getpid()}-{time.time_ns()}"
    if not stale or not _take_lock(now, token):
        return
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh", "--lock-token", token], **kw)
    except Exception:
        _release_lock(token)


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


def plugins_loaded_at(transcript_path, state=None):
    """When this session last loaded plugins: its first entry, or the last /reload-plugins.

    state: a dict the caller keeps between calls (it is updated in place). With it, a
    transcript that only grew since the last call is scanned from the end of the last
    scan, not from the start; a shorter file or another path is scanned in full.
    """
    state = {} if state is None else state
    path = str(transcript_path)
    try:
        size = os.stat(transcript_path).st_size
    except (OSError, TypeError, ValueError):
        return None
    start = reload_t = 0
    first_t = None
    if (state.get("path") == path and isinstance(state.get("size"), int) and 0 <= state["size"] <= size
            and _number(state.get("first"))):
        start, first_t, reload_t = state["size"], state["first"], state.get("reload")
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start)
            raw = f.read()
    except OSError:
        return None

    def ts_of(line):
        try:
            return _epoch(json.loads(line).get("timestamp"))
        except ValueError:
            return None

    i = raw.rfind(b"<command-name>/reload-plugins</command-name>")
    if i >= 0:
        s = raw.rfind(b"\n", 0, i) + 1
        end = raw.find(b"\n", i)
        t = ts_of(raw[s:end if end >= 0 else None])
        if t:
            reload_t = t
    if first_t is None:
        first_t = _first_timestamp(raw)
    # Resume at a line boundary, so a line still being written is read again, whole.
    state.update(path=path, size=start + raw.rfind(b"\n") + 1, first=first_t, reload=reload_t or None)
    return reload_t or first_t


def _first_timestamp(raw):
    for line in raw.split(b"\n", 50)[:50]:
        if not line.strip():
            continue
        try:
            t = _epoch(json.loads(line).get("timestamp"))
        except ValueError:
            continue
        if t:
            return t
    return None


def session_started_at(transcript_path):
    """The session's first transcript entry, as epoch seconds."""
    try:
        with open(transcript_path, "rb") as f:
            return _first_timestamp(f.read(65536))
    except (OSError, TypeError):
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


def read_mcp_logs(root, since=None, max_files=30):
    """{server: [events]} from log files written since `since`, or None when there are no logs.

    Every session in a project shares these folders, so the newest files are
    often another session's; read all the recent ones and let mcp_status pick.
    """
    try:
        dirs = [d for d in root.iterdir() if d.name.startswith("mcp-logs-")]
    except (OSError, AttributeError):
        return None
    logs = {}
    for d in dirs:
        events = []
        for f in sorted(d.glob("*.jsonl"), reverse=True)[:max_files]:
            try:
                if since is not None and f.stat().st_mtime < since:
                    continue
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


def count_skills(installed, enabled, claude_dir, project_dir=None):
    """Skills available to a session: enabled plugins' skills and commands, yours, the project's.

    A plugin whose plugin.json lists "skills" loads exactly those; otherwise
    each skills/<name>/SKILL.md. This matches what /reload-plugins reports,
    plus the user and project skill folders it leaves out.
    """
    n = 0
    for name, entries in (installed.get("plugins") or {}).items():
        if name not in enabled or not entries:
            continue
        root = Path(entries[-1].get("installPath") or "")
        listed = _read_json(root / ".claude-plugin" / "plugin.json").get("skills")
        if isinstance(listed, list):
            n += sum(1 for rel in listed if (root / rel / "SKILL.md").exists())
        else:
            n += len(list(root.glob("skills/*/SKILL.md")))
        n += len(list(root.glob("commands/*.md")))
    n += len(list(Path(claude_dir).glob("skills/*/SKILL.md")))
    if project_dir:
        n += len(list(Path(project_dir).glob(".claude/skills/*/SKILL.md")))
    return n


def health_marks(running, version, reload_plugins, mcp):
    """Health marks as [(text, level)] in line order: MCP servers, Claude Code, plugins.

    Level 0 is healthy (a green check), 1 needs attention (amber), 2 is a failure (red).
    A mark that needs attention says so by name: '▲ Gmail failed', '↑ 2.2 available',
    '~ reload'. Marks without data (no logs, no version, no plugin list) are left out.
    """
    marks = []
    if mcp is not None:
        ok, failed = mcp
        if failed:
            more = f" +{len(failed) - 1}" if len(failed) > 1 else ""
            marks.append((f"{RED}{ICON['warn']} {mid_trunc(failed[0], 12)}{more} failed{RST}", 2))
        elif ok:
            marks.append((f"{ic('mcp')}{DIM}mcp{RST} {len(ok)} {GREEN}{ICON['ok']}{RST}", 0))
    if running:
        state, newer = version
        cc = f"{ic('cc', MODEL)}{DIM}CC{RST} {running}"  # logo in Claude orange
        if state == "ok":
            marks.append((f"{cc} {GREEN}{ICON['ok']}{RST}", 0))
        elif state == "restart":  # newer build already downloaded; this window still runs the old one
            marks.append((f"{AMBER}{ICON['up']} restart{f' for {newer}' if newer else ''}{RST}", 1))
        elif state == "update":
            marks.append((f"{AMBER}{ICON['up']} {f'{newer} available' if newer else 'update'}{RST}", 1))
        else:
            marks.append((cc, 0))
    if reload_plugins is not None:
        if reload_plugins:
            marks.append((f"{AMBER}{ICON['reload']} reload{RST}", 1))
        else:
            marks.append((f"{ic('plugins')}{DIM}plugins{RST} {GREEN}{ICON['ok']}{RST}", 0))
    return marks


def health_state(data, cache=None):
    """(running, version, reload_plugins, mcp, skills) for health_marks and the skills item.

    cache: the render's ProbeCache; the probes are kept HEALTH_TTL seconds per session
    and project (SKILLS_TTL for the skill count, per project).
    """
    cache = _probe_cache(cache)
    running = data.get("version")
    ws = data.get("workspace") or {}
    key = json.dumps([data.get("session_id"), ws.get("project_dir") or data.get("cwd"),
                      data.get("transcript_path"), running])
    version, reload_plugins, mcp = cache.get("health:" + key, HEALTH_TTL, lambda: _health_probe(data, cache))

    def skill_count():
        settings, installed = _settings_and_plugins()
        return count_skills(installed, _enabled(settings), CLAUDE_DIR, ws.get("project_dir"))
    skills = cache.get("skills:" + json.dumps(ws.get("project_dir")), SKILLS_TTL, skill_count)
    return running, version, reload_plugins, mcp, skills


def _settings_and_plugins():
    return (_read_json(CLAUDE_DIR / "settings.json"),
            _read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json"))


def _enabled(settings):
    return {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}


def _health_probe(data, cache=None):
    """[version, reload_plugins, mcp]: the slow part of health_state (it reads the transcript and MCP logs).

    cache: the render's ProbeCache, which also keeps how far the transcript was scanned.
    """
    cache = _probe_cache(cache)
    settings, installed = _settings_and_plugins()
    running = data.get("version")
    channel = settings.get("autoUpdatesChannel") or "latest"
    published = (load_usage_cache().get("tags") or {}).get(channel)
    version = version_status(running, installed_versions(), published) if running else ("unknown", None)

    reload_plugins = None
    scan_key = "loaded:" + str(data.get("transcript_path"))
    scan = cache.peek(scan_key)
    loaded_at = plugins_loaded_at(data.get("transcript_path"), scan)
    cache.put(scan_key, scan)
    if loaded_at and installed:
        reload_plugins = plugins_needing_reload(installed, _enabled(settings), loaded_at)

    ws = data.get("workspace") or {}
    started = session_started_at(data.get("transcript_path"))
    logs = read_mcp_logs(mcp_log_root(ws.get("project_dir") or data.get("cwd")),
                         since=started - 60 if started else None)
    known = {k.split("@")[0] for k in (installed.get("plugins") or {})}
    mcp = mcp_status(logs, data.get("session_id"), known) if logs else None
    return [version, reload_plugins, mcp]


# --- subagents -----------------------------------------------------------------

SUBAGENT_STALE = 600  # seconds without a write before a subagent counts as gone
SUBAGENTS_SHOWN = 3


def fmt_model_id(model_id):
    """claude-haiku-5-5 -> Haiku 5.5; unknown ids pass through."""
    m = str(model_id or "").split("[")[0]
    parts = m.split("-")
    if len(parts) >= 3 and parts[0] == "claude" and all(x.isdigit() for x in parts[2:]):
        return f"{parts[1].capitalize()} {'.'.join(parts[2:])}"
    return m


def subagent_state(entries):
    """Model, effort, context tokens and running flag from a subagent's transcript entries."""
    state = {"model": None, "effort": None, "tokens": 0, "running": True}
    last = None
    for e in entries:
        if e.get("type") not in ("user", "assistant"):
            continue
        last = e
        if e["type"] != "assistant":
            continue
        msg = e.get("message") or {}
        state["model"] = msg.get("model") or state["model"]
        state["effort"] = e.get("effort") or state["effort"]
        u = msg.get("usage") or {}
        state["tokens"] = sum(int(u.get(k) or 0) for k in
                              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if last and last["type"] == "assistant" and (last.get("message") or {}).get("stop_reason") == "end_turn":
        state["running"] = False
    return state


def _tail_entries(path, max_bytes=262_144):
    """Parsed JSON lines from the end of a transcript (the first, maybe partial, line is dropped)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        raw = f.read()
    lines = raw.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def running_subagents(transcript_path, now):
    """Subagents of this session that are still working, newest first."""
    if not transcript_path:
        return []
    d = Path(str(transcript_path)[:-6] if str(transcript_path).endswith(".jsonl") else transcript_path) / "subagents"
    try:
        files = [(f.stat().st_mtime, f) for f in d.glob("agent-*.jsonl")]
    except OSError:
        return []
    agents = []
    for mtime, f in sorted(files, reverse=True):
        if now - mtime > SUBAGENT_STALE:
            continue
        try:
            state = subagent_state(_tail_entries(f))
        except OSError:
            continue
        if not state["running"]:
            continue
        meta = _read_json(f.with_name(f.name[:-6] + ".meta.json"))
        state["type"] = meta.get("agentType") or f.stem[6:]
        agents.append(state)
    return agents


def format_agents(agents):
    """Third line, shown only while subagents run."""
    if not agents:
        return ""
    parts = [f"{ic('agents')}{DIM}agents{RST} {len(agents)}"]
    for a in agents[:SUBAGENTS_SHOWN]:
        c = model_color(a.get("model"))
        s = f"{c}{a['type']}{RST}"
        if a.get("model"):
            s += f" {c}{fmt_model_id(a['model'])}{RST}"
        if a.get("effort"):
            s += f" {DIM}\u00b7{RST} {EFFORT.get(a['effort'], '')}{a['effort']}{RST}"
        if a.get("tokens"):
            s += f" {DIM}{fmt_tokens(a['tokens'])}{RST}"
        parts.append(s)
    if len(agents) > SUBAGENTS_SHOWN:
        parts.append(f"{DIM}+{len(agents) - SUBAGENTS_SHOWN}{RST}")
    return SEP.join(parts)


# --- workspace probes --------------------------------------------------------

def git_info(cwd, cache=None):
    """(branch, flags) for where_item; None outside a repo, '' without a directory.

    cache: the render's ProbeCache; git status is kept GIT_TTL seconds per directory.
    """
    if not cwd or not Path(cwd).exists():
        return ""
    status = _probe_cache(cache).get("git:" + str(cwd), GIT_TTL, lambda: git_status(cwd))
    if not status:
        return None
    branch, stg, mod, unt = status
    flags = []
    if stg:
        flags.append(f"{GREEN}+{stg}{RST}")
    if mod:
        flags.append(f"{AMBER}~{mod}{RST}")
    if unt:
        flags.append(f"{DIM}?{unt}{RST}")
    return branch, flags


def git_status(cwd):
    """[branch, staged, modified, untracked] from one git call, or None outside a repo."""
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
        return None
    return [branch, stg, mod, unt]


MIN_NAME = 14      # a shortened project or branch name keeps at least this many characters
SHORT_BRANCH = 11  # a tight line shows 'live-usage…': this many characters, then the ellipsis


def _where_text(proj, branch, flags):
    parts = []
    if proj:
        parts.append(f"{ic('project', PROJECT)}{PROJECT}{proj}{RST}")
    if branch:
        color = AMBER if flags else GREEN
        parts.append(" ".join([f"{ic('branch', color)}{color}{branch}{RST}"] + list(flags)))
    return f" {DIM}›{RST} ".join(parts)


def shrink_where(proj, branch, flags, width):
    """(project, branch) with the longer one losing characters from its middle
    ('claude-co…statusline') until 'project › branch flags' fits `width`. Neither
    goes below MIN_NAME characters. width None: unchanged."""
    p_len, b_len = len(proj), len(branch)
    p, b = proj, branch
    while width and visible_width(_where_text(p, b, flags)) > width:
        if b_len >= p_len and b_len > MIN_NAME:
            b_len -= 1
        elif p_len > MIN_NAME:
            p_len -= 1
        elif b_len > MIN_NAME:
            b_len -= 1
        else:
            break  # as short as it gets
        p, b = mid_trunc(proj, p_len), mid_trunc(branch, b_len)
    return p, b


def where_item(proj, git, width=None):
    """'project › branch flags', shrunk to `width` by shrink_where. git: git_info()'s result."""
    branch, flags = git or ("", [])
    return _where_text(*shrink_where(proj, branch, flags, width), flags)


def short_branch(branch):
    """'live-usage-and-redesign' -> 'live-usage…'; short names stay."""
    return branch if len(branch) <= SHORT_BRANCH + 1 else branch[:SHORT_BRANCH] + "…"


# --- session name --------------------------------------------------------------

NAME_MAX = 28           # longest name shown (a hyphenated slug)
TAIL_BYTES = 262_144    # how much of the transcript's end is searched for a title
TITLE_KEYS = (("ai-title", "aiTitle"), ("agent-name", "agentName"), ("custom-title", "customTitle"))


def slugify(text, cap=NAME_MAX):
    """'Statusline.py: usage accuracy' -> 'statusline-py-usage-accuracy', cut at a word boundary
    within `cap` characters (a single longer word is cut hard)."""
    slug = re.sub(r"[\W_]+", "-", str(text).lower()).strip("-")
    if len(slug) <= cap:
        return slug
    if slug[cap] == "-":
        return slug[:cap]
    head = slug[:cap]
    cut = head.rfind("-")
    return head[:cut] if cut > 0 else head


def tail_title(path, max_bytes=TAIL_BYTES):
    """The newest session title in the last `max_bytes` of a transcript, '' when there is none.

    A title is an {"type": "ai-title", "aiTitle": ...} entry (or agent-name / custom-title).
    Transcripts can be 100 MB, so only the end is read; the first, maybe cut, line and a
    half-written last line are skipped because they do not parse.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        raw = f.read(max_bytes)
    lines = raw.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    markers = [f'"{kind}"'.encode() for kind, _ in TITLE_KEYS]
    for line in reversed(lines):
        if not any(m in line for m in markers):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for kind, key in TITLE_KEYS:
            title = entry.get(key) if entry.get("type") == kind else None
            if isinstance(title, str) and title.strip():
                return title.strip()
    return ""


def session_name(data, cache=None):
    """The session's name as a short slug, or '' when it has none.

    The payload's session_name (set with /rename) wins; otherwise the newest title in the
    transcript's tail. The transcript is only searched again when its size changed: the
    result is kept in the probe cache under (path, size), and a grown transcript with no
    title in its tail keeps the name found before.
    """
    given = data.get("session_name")
    if isinstance(given, str) and given.strip():
        return slugify(given)
    path = data.get("transcript_path")
    if not path:
        return ""
    try:
        size = os.stat(path).st_size
    except (OSError, ValueError, TypeError):
        return ""
    cache = _probe_cache(cache)
    key = "name:" + str(path)
    saved = cache.peek(key)
    old = saved.get("name") if isinstance(saved.get("name"), str) else ""
    if saved.get("size") == size and "name" in saved:
        if (cache.age(key) or 0) > NAME_REFRESH:
            cache.put(key, saved)  # keep the entry from expiring while the transcript sits still
        return old
    try:
        title = tail_title(path)
    except OSError:
        title = ""
    name = slugify(title) if title else old
    cache.put(key, {"size": size, "name": name})
    return name


def line_width():
    """Columns available to the status line, or None when the window width is unknown."""
    try:
        width = int(os.environ.get("COLUMNS") or 0) - EDGE
    except ValueError:
        return None
    return width if width > 20 else None


def venv_tag():
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda and conda != "base":
        return f"{ic('venv', CYAN)}{DIM}env:{RST}{CYAN}{conda}{RST}"
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return f"{ic('venv', CYAN)}{DIM}venv:{RST}{CYAN}{Path(venv).name}{RST}"
    return ""


def open_ports(ports=None, timeout=0.02):
    """Ports on 127.0.0.1 that accept a connection within `timeout` seconds.

    All at once, with non-blocking connects and select(): on Windows a closed port
    never refuses quickly, so each one waits out the whole timeout. No threads.
    """
    import select
    import socket
    ports = COMMON_PORTS if ports is None else ports
    pending, up, socks = {}, set(), []
    try:
        for port in ports:
            s = socket.socket()
            pending[s] = port
            s.setblocking(False)
            err = s.connect_ex(("127.0.0.1", port))
            if err == 0:
                up.add(port)
            elif err in (10035, 115, 36, 11, 35):  # WSAEWOULDBLOCK, EINPROGRESS, EAGAIN (Linux, macOS)
                socks.append(s)  # still connecting; anything else was refused at once
        deadline = time.monotonic() + timeout
        while socks:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _, writable, failed = select.select([], socks, socks, left)
            if not writable and not failed:
                break
            for s in set(writable) | set(failed):
                if s not in failed and s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    up.add(pending[s])
                socks.remove(s)
    except OSError:
        pass
    finally:
        for s in pending:
            s.close()
    return [p for p in ports if p in up]


def ports_tag(cache=None):
    """':3000,5173' for the dev ports that answer. cache: kept PORTS_TTL seconds."""
    up = _probe_cache(cache).get("ports:" + ",".join(map(str, COMMON_PORTS)), PORTS_TTL, open_ports)
    return f"{ic('ports', GREEN)}{GREEN}:{','.join(str(p) for p in up)}{RST}" if up else ""


# --- layout ------------------------------------------------------------------

def model_head(data):
    """'Opus 5.5 · high' in the model family's color and the effort's color."""
    model = data.get("model", {}) or {}
    name = model.get("display_name", "Claude")
    mc = model_color(model.get("id") or name)
    head = f"{ic('model', mc)}{mc}{name}{RST}"
    level = (data.get("effort") or {}).get("level")
    if level:
        head += f" {DIM}·{RST} {EFFORT.get(level, '')}{level}{RST}"
    return head


def session_time(data):
    """Session time: clock icon and number, or 'session 12m' without icons; '' before any time."""
    dur = fmt_duration(int((data.get("cost", {}) or {}).get("total_duration_ms", 0) or 0))
    if not dur:
        return ""
    return f"{ic('duration')}{DIM}{dur}{RST}" if ICON.get("duration") else f"{DIM}session {dur}{RST}"


def name_tag(name):
    """The session name as a tag: quoted in plain mode, after a tag icon with Nerd icons."""
    if ICON.get("tag"):
        return f"{ic('tag', CYAN)}{CYAN}{name}{RST}"
    return f"{CYAN}“{name}”{RST}"


def gather_extras(data, cache=None):
    """The probed items for format_statusline: project, git, session name, health, ports, venv.

    Everything behind the probe cache (git 3 s, ports 10 s, health 10 s, skills 60 s, the
    name until the transcript grows). A probe that fails only leaves its item out.
    """
    ws = data.get("workspace", {}) or {}
    cwd = ws.get("current_dir") or ws.get("project_dir") or data.get("cwd") or os.getcwd()
    extras = {"proj": Path(cwd).name if cwd else "", "git": None, "name": "", "marks": [],
              "skills": None, "ports": "", "venv": ""}
    for key, probe in (("git", lambda: git_info(cwd, cache)),
                       ("name", lambda: session_name(data, cache)),
                       ("venv", venv_tag),
                       ("ports", lambda: ports_tag(cache))):
        try:
            extras[key] = probe()
        except Exception:
            pass
    if SHOW_HEALTH:
        try:
            running, version, reload_plugins, mcp, skills = health_state(data, cache)
            extras["marks"] = health_marks(running, version, reload_plugins, mcp)
            extras["skills"] = skills
        except Exception:
            pass
    return extras


def format_statusline(data, usage=None, now=None, show_usage=True, extras=None, width=None):
    """The status line: ONE line, never wider than `width` columns (None: no limit).

    usage: merge_usage() result, or None when nothing is known yet. extras: gather_extras()'s
    result; without it only the session's own items show. Left to right:

        project › branch ~N “session-name” │ model · effort │ ctx % bar tokens │
        5h % bar reset │ 7d % bar reset │ cost │ session time │ health marks │
        skills │ venv │ :ports

    The line is made for a 120-column window. When it does not fit `width`, things go in
    this order, each only if the line is still too wide:

      1. venv and ports, 2. skills, 3. the 5h/7d reset countdowns, 4. session time,
      5. healthy health marks, last one first (plugins, then CC, then mcp),
      6. marks that need attention, last one first, each shown by name ('▲ Gmail failed'); when
         the last text goes, one bare marker (▲, red for a failure, else amber) stands in for
         all of them, and it outlasts everything below except model and percentages,
      7. the session name,
      8. the branch: first project and branch are squeezed into the room left (the longer
         loses characters from its middle, neither below 14), then the branch is cut to
         'live-usage…', then dropped (with its dirty counts),
      9. the project: squeezed down to 14 characters as the room shrinks, then dropped,
     10. the ctx tokens, 11. the bars (7d, then 5h, then ctx), 12. the cost, 13. the bare
         marker.

    Model and the three percentages always stay. Session time sits between the countdowns
    and the health marks: it is cheap and the least useful of them.
    """
    now = time.time() if now is None else now
    cw = data.get("context_window", {}) or {}
    ex = extras or {}

    meters = [("ctx", context_pct(cw), "ctx", context_tokens(cw))]
    if show_usage:
        usage = usage or {}
        for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
            w = usage.get(key)
            left = fmt_until(w["resets_at"] - now) if w and w["resets_at"] else ""
            meters.append((label, w["pct"] if w else None, key, left))
    head = model_head(data)
    cost = fmt_cost(float((data.get("cost", {}) or {}).get("total_cost_usd", 0.0) or 0.0))
    session = session_time(data)
    tag = name_tag(ex["name"]) if ex.get("name") else ""
    skills = f"{ic('skills')}{DIM}skills{RST} {ex['skills']}" if ex.get("skills") else ""
    marks = list(ex.get("marks") or [])
    marker = ""
    if any(level for _, level in marks):
        marker = f"{RED if any(level == 2 for _, level in marks) else AMBER}{ICON['warn']}{RST}"

    proj = ex.get("proj") or ""
    branch, flags = ex.get("git") or ("", [])

    def where(s, rest):
        """The project › branch item. Once the branch is being given up ('fit', 'short' or
        gone) the item is squeezed into what `rest` (the other items, as they would be
        joined) leaves of the width; the project never goes below MIN_NAME characters."""
        p, b, f = proj, branch, flags
        if s["branch"] is None:
            b, f = "", []
        elif s["branch"] == "short":
            b = short_branch(b)
        if s["branch"] != "full" and width:
            taken = visible_width(SEP.join(rest)) + visible_width(SEP)
            if s["name"] and tag:
                taken += visible_width(tag) + 1
            p, b = shrink_where(p, b, f, width - taken)
        if s["proj"] is None:
            p = ""
        return _where_text(p, b, f)

    def items(s):
        out = [head]
        for i, (label, p, icon, suffix) in enumerate(meters):
            show = s["tokens"] if i == 0 else s["countdown"]
            out.append(meter(label, p, icon, with_bar=i < s["bars"], suffix=suffix if show else ""))
        if s["cost"]:
            out.append(cost)
        if s["session"] and session:
            out.append(session)
        out += [text for i, (text, _) in enumerate(marks) if i not in s["dropped"]]
        if s["marker"] and marker:
            out.append(marker)
        if s["skills"] and skills:
            out.append(skills)
        if s["extras"]:
            out += [x for x in (ex.get("venv"), ex.get("ports")) if x]
        first = " ".join(x for x in (where(s, out), tag if s["name"] else "") if x)
        return [first] + out if first else out

    # The drop order, as patches applied one after the other to the state.
    steps = [{"extras": False}, {"skills": False}, {"countdown": False}, {"session": False}]
    healthy = [i for i, (_, level) in enumerate(marks) if level == 0]
    needy = [i for i, (_, level) in enumerate(marks) if level]
    steps += [{"dropped": i} for i in reversed(healthy)]
    steps += [{"dropped": i, "marker": k == len(needy) - 1} for k, i in enumerate(reversed(needy))]
    steps += [{"name": False}, {"branch": "fit"}, {"branch": "short"}, {"branch": None},
              {"proj": None},
              {"tokens": False}, {"bars": 2}, {"bars": 1}, {"bars": 0}, {"cost": False}, {"marker": False}]

    state = {"extras": True, "skills": True, "countdown": True, "session": True, "dropped": set(),
             "marker": False, "name": True, "branch": "full", "proj": "full", "tokens": True,
             "bars": len(meters), "cost": True}
    best = items(state)
    for step in [None] + steps:
        if step:
            step = dict(step)
            if "dropped" in step:
                state["dropped"] = state["dropped"] | {step.pop("dropped")}
            state.update(step)
            best = items(state)
        if width is None or visible_width(SEP.join(best)) <= width:
            break
    return SEP.join(best)


def main():
    if "--refresh" in sys.argv:
        # The spawning render created the lock and passed its token; a manual run takes its own.
        token = sys.argv[sys.argv.index("--lock-token") + 1] if "--lock-token" in sys.argv[:-1] else None
        if token is None:
            token = f"{os.getpid()}-{time.time_ns()}"
            if not _take_lock(time.time(), token):
                return
        _arm_deadline(token)
        try:
            refresh_cache()
        finally:
            _release_lock(token)
        return
    mode = os.environ.get("STATUSLINE_ICONS", "none")
    if "--icons" in sys.argv[:-1]:
        mode = sys.argv[sys.argv.index("--icons") + 1]
    set_icons(effective_icons(mode))
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
        show_usage = True  # unknown windows read "5h --", never vanish
        if os.environ.get("STATUSLINE_LIVE_USAGE", "1") != "0":
            cache = load_usage_cache()
            live = cache.get("usage")
            if read_oauth_token() or live:
                maybe_refresh(cache, now)
        try:
            usage = merge_usage(stdin_rl, live, now)
        except Exception:
            usage = merge_usage(stdin_rl, None, now)  # a bad cache must not hide the live numbers
        known = any(usage.values())
        probes = ProbeCache(CLAUDE_DIR / PROBE_CACHE_NAME, now)
        # One line, fitted to the window; every slow probe sits behind the probe cache.
        line = format_statusline(data, usage if known else None, now, show_usage,
                                 gather_extras(data, probes), line_width())
        if SHOW_AGENTS:
            try:
                agents = format_agents(running_subagents(data.get("transcript_path"), now))
                if agents:
                    line += "\n" + agents
            except Exception:
                pass
        print(line)
        probes.save()  # never raises
    except Exception:
        print("Claude | --")
    sys.exit(0)


if __name__ == "__main__":
    main()
