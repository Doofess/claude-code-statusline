# /// script
# requires-python = ">=3.10"
# ///

"""Unified status line for Claude Code.

Sections (left to right):
    model | ctx:N% bar | $cost | 5h:N% 7d:N% | duration | project | git | venv | :ports

Every section after model + ctx is auto-detected and shown only when relevant.
Standalone: no external dependencies.

Install:
    1. Drop this file at ~/.claude/statusline.py
    2. Add to ~/.claude/settings.json:
           "statusLine": {
               "type": "command",
               "command": "python ~/.claude/statusline.py"
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
from pathlib import Path

# ANSI colors
G = "\033[32m"    # green  — healthy
Y = "\033[33m"    # yellow — warning / dirty git
R = "\033[31m"    # red    — critical
C = "\033[36m"    # cyan   — model, venv
M = "\033[35m"    # magenta — project name
DIM = "\033[2m"   # dim    — session duration
RST = "\033[0m"

# Edit this list to match the dev ports you care about.
COMMON_PORTS = [3000, 3001, 5173, 5174, 8000, 8080, 8081, 8086, 9086, 9087, 54321]


def color_pct(p):
    if p >= 80:
        return R
    if p >= 50:
        return Y
    return G


def bar(p, width=10):
    filled = max(0, min(width, round(p / 100 * width)))
    return "\u2588" * filled + "\u2591" * (width - filled)


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


def fmt_cost(c):
    return f"${c:.1f}" if c >= 10 else f"${c:.2f}"


def run(cmd, timeout=1.0):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def git_info(cwd):
    if not cwd or not Path(cwd).exists():
        return ""
    if not run(["git", "-C", cwd, "rev-parse", "--git-dir"]):
        return ""
    branch = run(["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"]).strip()
    if not branch:
        branch = run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"]).strip()
    if not branch:
        return ""
    if len(branch) > 22:
        branch = branch[:20] + ".."
    stg = run(["git", "-C", cwd, "diff", "--cached", "--name-only"]).count("\n")
    mod = run(["git", "-C", cwd, "diff", "--name-only"]).count("\n")
    unt = run(["git", "-C", cwd, "ls-files", "--others", "--exclude-standard"]).count("\n")
    flags = []
    if stg:
        flags.append(f"+{stg}")
    if mod:
        flags.append(f"~{mod}")
    if unt:
        flags.append(f"?{unt}")
    if flags:
        return f"{Y}{branch}[{' '.join(flags)}]{RST}"
    return f"{G}{branch}{RST}"


def venv_tag():
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda and conda != "base":
        return f"{C}env:{conda}{RST}"
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        return f"{C}venv:{Path(venv).name}{RST}"
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
    up = [p for p in COMMON_PORTS if port_up(p)]
    return f"{G}:{','.join(str(p) for p in up)}{RST}" if up else ""


def format_statusline(data):
    cw = data.get("context_window", {})
    cost = data.get("cost", {})
    model = data.get("model", {})
    rl = data.get("rate_limits", {})
    ws = data.get("workspace", {}) or {}

    name = model.get("display_name", "Claude")
    used = float(cw.get("used_percentage", 0) or 0)
    ctx = f"{color_pct(used)}ctx:{used:.0f}% {bar(used)}{RST}"

    total_cost = float(cost.get("total_cost_usd", 0.0) or 0.0)
    cost_s = fmt_cost(total_cost)

    dur = fmt_duration(int(cost.get("total_duration_ms", 0) or 0))
    dur_s = f"{DIM}\u23f1{dur}{RST}" if dur else ""

    five = float(rl.get("five_hour", {}).get("used_percentage", 0.0) or 0.0)
    seven = float(rl.get("seven_day", {}).get("used_percentage", 0.0) or 0.0)
    rl_s = f"{color_pct(five)}5h:{five:.0f}%{RST} {color_pct(seven)}7d:{seven:.0f}%{RST}"

    cwd = ws.get("current_dir") or ws.get("project_dir") or data.get("cwd") or os.getcwd()
    proj = Path(cwd).name if cwd else ""
    proj_s = f"{M}{proj}{RST}" if proj else ""

    parts = [f"{C}{name}{RST}", ctx, cost_s, rl_s]
    if dur_s:
        parts.append(dur_s)
    if proj_s:
        parts.append(proj_s)
    git_s = git_info(cwd)
    if git_s:
        parts.append(git_s)
    venv_s = venv_tag()
    if venv_s:
        parts.append(venv_s)
    ports_s = ports_tag()
    if ports_s:
        parts.append(ports_s)
    return " | ".join(parts)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        print(format_statusline(data))
    except Exception:
        print("Claude | --")
    sys.exit(0)


if __name__ == "__main__":
    main()
