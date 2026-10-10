# claude-code-statusline

A unified, modular status line for [Claude Code](https://claude.com/claude-code) — the same informative bar in every project, zero per-repo config.

```
Opus 4.6 · high │ ctx ▰▰▰▱▱▱▱▱ 42% 84k/200k │ 5h ▰▰▰▰▱▱▱▱ 56% 14m │ 7d ▰▰▰▰▱▱▱▱ 48% 5d2h │ $0.37 │ 12m │ my-app › main +2 ~5 ?1 │ venv:.venv │ :3000,8080
cc 2.1.296 ✓ │ plugins ✓ │ mcp 16 ▲ discord
```

## Why

Claude Code ships with a minimal status line. If you use it across multiple repos, you either stare at a bar that says almost nothing or you write a bespoke shell script for every project. Neither scales.

This is one Python file that gives you vital session info everywhere and quietly auto-detects anything project-specific.

## What it shows

| Section | Meaning |
|---|---|
| **model · effort** | Model display name, colored by family (Opus orange, Sonnet blue, Haiku teal, Fable pink), then the reasoning effort level (low → max) when set |
| **ctx ▰▰▱▱ N% used/size** | Context window usage as a percentage and in tokens — shifts green → amber → red as it fills |
| **5h / 7d** | Plan usage for both windows, account-wide (see below), plus time until each window resets. `--` until the first reading arrives |
| **$X.XX** | Session cost in USD (bold green) |
| **duration** | Session wall-clock time |
| **project › git** | Working directory name, then branch + dirty counts: `+staged ~modified ?untracked`. Branch is green when clean, amber when dirty |
| **cc** *(line 2)* | Claude Code version: `✓` up to date, `↑ X` newer release on your update channel, `↑ X restart` already installed and waiting for a restart |
| **plugins** *(line 2)* | `✓`, or `↑ N /reload-plugins` when N enabled plugins were updated on disk after this session loaded them |
| **mcp** *(line 2)* | MCP servers connected in this session; `▲ name` for any that failed to connect (read from Claude Code's per-session MCP logs, no extra connections) |
| **agents** *(line 3, off by default: `SHOW_AGENTS = True`)* | Each running subagent: type and model in the model's color, effort, and context tokens. Up to 3, then `+N` |
| **venv** | Active Python virtualenv or conda environment |
| **:ports** | Common dev ports currently listening (3000, 5173, 8000, 8080, 9086, …) |

Every section after `model` and `ctx` is auto-detected — a Rust repo with no venv simply won't render a venv tag.

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
- **Extra lines** — `SHOW_HEALTH = False` / `SHOW_AGENTS = False` turn off the health and subagent lines.
- **Model colors** — `MODEL_COLORS` maps model families to colors.
- **Usage refresh** — `USAGE_TTL` sets how often live usage is fetched (default 60 s). `STATUSLINE_LIVE_USAGE=0` turns live fetching off.
- **Sections** — add your own by appending to `parts` in `format_statusline()`.

No framework, no plugin system — one function per section, all readable in a few minutes.

## How it works

Claude Code pipes a JSON blob to whatever command you set as `statusLine`, containing the current session's model, context-window usage, cost, rate-limits, and workspace info. This script reads that JSON from stdin, runs a handful of fast local probes (git, env vars, port checks), and prints the formatted bar to stdout.

Each probe has a tight timeout: one `git status` call (≤1 s) and port checks run in parallel (50 ms). A render takes ~200 ms on Windows, which matters because Claude Code cancels a render that is still running when the next update arrives.

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
