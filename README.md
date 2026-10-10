# claude-code-statusline

A unified, modular status line for [Claude Code](https://claude.com/claude-code) — the same informative bar in every project, zero per-repo config.

```
my-app › main ~2 “fix-login-redirect” │ Opus 4.6 · high │ ctx 42% █████ 84k/200k │ 5h 56% █████ 14m │ 7d 48% █████ 5d2h │ $0.37 │ session 12m │ mcp 16 ✓ │ CC 2.1.296 ✓ │ plugins ✓ │ skills 69 │ venv:.venv │ :3000,8080
```

One line, always. It is made for a 120-column window, fits itself to the width it is given, and never wraps.

## Why

Claude Code ships with a minimal status line. If you use it across multiple repos, you either stare at a bar that says almost nothing or you write a bespoke shell script for every project. Neither scales.

This is one Python file that gives you vital session info everywhere and quietly auto-detects anything project-specific.

## What it shows

Left to right:

| Item | Meaning |
|---|---|
| **project › branch ~N** | Working directory name, then the git branch with dirty counts: `+staged ~modified ?untracked`. Project is violet; the branch is green when clean, amber when dirty |
| **“session-name”** | The session's name, only when it has one: the payload's `session_name` (set with `/rename`), else the newest `ai-title` / `agent-name` / `custom-title` entry in the last 256 KB of the transcript, as a lowercase hyphen slug of at most 28 characters. Kept in the probe cache by transcript size, so a render only reads the file again when it grew. A tag icon replaces the quotes with Nerd icons |
| **model · effort** | Model display name, colored by family (Opus orange, Sonnet blue, Haiku teal, Fable pink), then the reasoning effort level (low → max) when set |
| **ctx N% bar used/size** | Context window usage as a percentage, a bar and the tokens — shifts green → yellow → orange → red as it fills |
| **5h / 7d** | Plan usage for both windows, account-wide (see below): percentage, bar, and the time until the window resets. `--` until the first reading arrives |
| **$X.XX** | Session cost in USD (bold green) |
| **session time** | Session wall-clock time: `session 12m`, or a clock icon with Nerd icons |
| **health marks** | `mcp 3 ✓` (servers connected in this session, read from Claude Code's per-session MCP logs), `CC 2.1.296 ✓` (Claude Code version), `plugins ✓`. Each turns into a named warning when something is wrong: `▲ Gmail failed` (red), `↑ 2.2 available` or `↑ restart for 2.2` (amber), `~ reload` (amber, enabled plugins changed on disk after this session loaded them) |
| **skills N** | Skills available to the session |
| **venv / :ports** | Active Python virtualenv or conda environment, and common dev ports currently listening (3000, 5173, 8000, 8080, 9086, …). Only shown when something is found |
| **agents** *(a second line, off by default: `SHOW_AGENTS = True`)* | Each running subagent: type and model in the model's color, effort, and context tokens. Up to 3, then `+N` |

### Fitting the window

The line must be no wider than `COLUMNS` minus 4 (Claude Code pads the right edge). A wide window shows everything; a narrow one drops items in this order, each only while the line is still too wide:

1. venv and ports
2. skills
3. the 5h / 7d reset countdowns
4. session time
5. healthy health marks (plugins, then CC, then mcp)
6. health marks that need attention, last first, each by name; when the last text goes, one bare `▲` stands in for all of them (red for a failure, else amber). It outlasts everything below except model and percentages, so a failure is never invisible
7. the session name
8. the branch: first project and branch are squeezed into the room left (the longer loses characters from its middle, neither below 14), then the branch is cut to `live-usage…`, then dropped
9. the project: squeezed down to 14 characters, then dropped
10. the ctx tokens
11. the bars (7d, then 5h, then ctx)
12. the cost
13. the bare `▲`

Model and the three percentages always stay.

### Icons

Plain mode (the default) uses only glyphs that Consolas, Cascadia, Menlo and DejaVu have: bars of 5 `█` cells, `✓ ↑ ▲ ~` for the marks. With `--icons nerd` every item gets a Nerd Font icon and the bars become 3 slanted segments. The classic Windows console host cannot draw Nerd Font glyphs, so `nerd` falls back to plain there (set `STATUSLINE_FORCE_ICONS=1` to skip that check).

Everything slow is auto-detected and cached (git 3 s, ports 10 s, health 10 s, skill count 60 s), so a render stays at a few tens of milliseconds.

## Install

**Requirements:** Python 3.10+, a terminal that supports 256-color (Windows Terminal, iTerm2, macOS Terminal, modern Gnome Terminal — all fine out of the box). No Nerd Font needed: every glyph is in stock Cascadia, Menlo and DejaVu.

### Icons (optional)

With a [Nerd Font](https://www.nerdfonts.com/) set as your terminal font (e.g. *CaskaydiaMono Nerd Font*, the Cascadia you already have plus icons), add `--icons nerd` to the command:

```json
"command": "python ~/.claude/statusline.py --icons nerd"
```

`STATUSLINE_ICONS=nerd` works too. Without it the bar uses plain text, so nothing renders as boxes.

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
    "command": "python ~/.claude/statusline.py",
    "refreshInterval": 10
  }
}
```

`refreshInterval` (seconds) re-renders the bar while the session is idle; without it Claude Code only redraws on new messages, so usage numbers freeze between turns.

If you use [uv](https://github.com/astral-sh/uv), swap `python` for `uv run` — start-up is ~10× faster.

### 3. Restart Claude Code

New session, new bar.

## Customize

Everything lives in the top of `statusline.py`.

- **Ports** — edit `COMMON_PORTS` to match your stack.
- **Colors** — tweak the 256-color constants (`MODEL`, `PROJECT`, `GREEN`, `AMBER`, `CYAN`) and the `STAGES` / `RED` used for percentages (green, yellow from 50%, orange from 70%, red from 85%).
- **Health and subagents** — `SHOW_HEALTH = False` leaves out the health marks and the skill count; `SHOW_AGENTS = True` adds the subagent line.
- **Model colors** — `MODEL_COLORS` maps model families to colors.
- **Usage refresh** — `USAGE_TTL` sets how often live usage is fetched (default 60 s). `STATUSLINE_LIVE_USAGE=0` turns live fetching off.
- **Items and drop order** — `format_statusline()` builds the items and lists the drop order as patches (`steps`); its docstring is the reference.

No framework, no plugin system — all readable in a few minutes.

## How it works

Claude Code pipes a JSON blob to whatever command you set as `statusLine`, containing the current session's model, context-window usage, cost, rate-limits, and workspace info. This script reads that JSON from stdin, runs a handful of fast local probes (git, env vars, port checks), and prints the formatted bar to stdout.

Each probe has a tight timeout (one `git status` call ≤1 s, port checks in parallel, 20 ms) and keeps its result in a small shared cache file (`~/.claude/statusline-probe-cache.json`) for a few seconds. A warm render takes about 40 ms on Windows (a cold cache about 110 ms), which matters because Claude Code cancels a render that is still running when the next update arrives. The status line runs fine as `python -S ~/.claude/statusline.py --icons nerd`: it needs only the standard library.

### Why the usage numbers are accurate

The `rate_limits` Claude Code pipes in come from *this session's* last API reply. Your plan quota is shared by every session, claude.ai and the desktop app, so a quiet session shows old numbers until it next talks to the API, and a brand-new session has none at all.

So the script also reads the endpoint behind `/usage` (with the Claude subscription token Claude Code already stores in `~/.claude/.credentials.json`, or the macOS Keychain). It does this in a detached background process, at most once per `USAGE_TTL`, and caches the result in `~/.claude/statusline-usage.json` — rendering never waits on the network. Usage only grows within a window, so for each window the bar shows the higher of the two readings, and a reading for a newer window replaces one whose window has reset.

The token never leaves your machine except to `api.anthropic.com`. If you use an API key instead of a subscription, there is no token, nothing is fetched, and the usage section is hidden. The endpoint is undocumented and could change; if it fails, the bar falls back to the piped-in numbers.

## Project-specific overrides

Claude Code honors `.claude/settings.local.json` inside each project. If one repo genuinely needs a different bar (e.g. embedded build-status of a data pipeline), point that file at a project-specific script. Everywhere else inherits the global version.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The script is small on purpose; please keep additions modular and opt-in.

Tests: `python -m unittest test_statusline` (stdlib only).
