# claude-code-statusline

A unified, modular status line for [Claude Code](https://claude.com/claude-code) — the same informative bar in every project, zero per-repo config.

```
Opus 4.6 | ctx:42% ████░░░░░░ | $0.37 | 5h:24% 7d:41% | ⏱12m | my-app | main[+2 ~5 ?1] | venv:.venv | :3000,8080
```

## Why

Claude Code ships with a minimal status line. If you use it across multiple repos, you either stare at a bar that says almost nothing or you write a bespoke shell script for every project. Neither scales.

This is one Python file that gives you vital session info everywhere and quietly auto-detects anything project-specific.

## What it shows

| Section | Meaning |
|---|---|
| **model** | Model display name (cyan) |
| **ctx:N% ▓▓▓▓░░** | Context window usage — green <50, yellow <80, red ≥80 |
| **$X.XX** | Session cost in USD |
| **5h / 7d** | Rate-limit usage for both windows, color-coded |
| **⏱duration** | Session wall-clock time |
| **project** | Current working directory name (magenta) |
| **git** | Branch + dirty counts: `+staged ~modified ?untracked`. Green when clean, yellow when dirty |
| **venv** | Active Python virtualenv or conda environment |
| **:ports** | Common dev ports currently listening (3000, 5173, 8000, 8080, 9086, …) |

Every section after `model` and `ctx` is auto-detected — a Rust repo with no venv simply won't render a venv tag.

## Install

**Requirements:** Python 3.10+, a terminal that supports 256-color (Windows Terminal, iTerm2, modern Gnome Terminal — all fine out of the box).

### 1. Drop the file

```bash
# macOS / Linux
curl -o ~/.claude/statusline.py https://raw.githubusercontent.com/roeimichael/claude-code-statusline/main/statusline.py
```

```powershell
# Windows (PowerShell)
iwr https://raw.githubusercontent.com/roeimichael/claude-code-statusline/main/statusline.py -OutFile "$env:USERPROFILE\.claude\statusline.py"
```

Or just copy `statusline.py` from this repo to `~/.claude/statusline.py`.

### 2. Wire it into settings

Edit `~/.claude/settings.json` and add:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python ~/.claude/statusline.py"
  }
}
```

If you use [uv](https://github.com/astral-sh/uv), swap `python` for `uv run` — start-up is ~10× faster.

### 3. Restart Claude Code

New session, new bar.

## Customize

Everything lives in the top of `statusline.py`.

- **Ports** — edit `COMMON_PORTS` to match your stack.
- **Colors** — tweak the ANSI constants (`G`, `Y`, `R`, `C`, `M`, `DIM`).
- **Thresholds** — `color_pct()` controls when percentages turn yellow/red.
- **Sections** — add your own by appending to `parts` in `format_statusline()`.

No framework, no plugin system — one function per section, all readable in a few minutes.

## How it works

Claude Code pipes a JSON blob to whatever command you set as `statusLine`, containing the current session's model, context-window usage, cost, rate-limits, and workspace info. This script reads that JSON from stdin, runs a handful of fast local probes (git, env vars, port checks), and prints the formatted bar to stdout.

Each probe has a tight timeout (≤1 s for git, 50 ms per port). Total render time stays well under one second even on Windows.

## Project-specific overrides

Claude Code honors `.claude/settings.local.json` inside each project. If one repo genuinely needs a different bar (e.g. embedded build-status of a data pipeline), point that file at a project-specific script. Everywhere else inherits the global version.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The script is small on purpose; please keep additions modular and opt-in.
