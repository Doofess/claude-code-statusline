"""Tests for statusline.py. Run: python -m unittest test_statusline"""

import pathlib
import re
import sys
import unittest

import statusline as sl

NOW = 1_800_000_000.0
HOUR = 3600
ANSI = re.compile(r"\033\[[0-9;]*m")


def plain(s):
    return ANSI.sub("", s)


def stdin_rl(five=None, seven=None):
    """rate_limits as Claude Code pipes it: epoch-second resets_at."""
    rl = {}
    if five:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven:
        rl["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    return rl


def live(five=None, seven=None):
    """Body of the /api/oauth/usage endpoint: ISO resets_at, fractional seconds."""
    from datetime import datetime, timezone

    def win(w):
        if not w:
            return None
        iso = datetime.fromtimestamp(w[1], timezone.utc).isoformat()
        return {"utilization": w[0], "resets_at": iso}

    return {"five_hour": win(five), "seven_day": win(seven)}


class MergeUsage(unittest.TestCase):
    def test_other_session_usage_wins_over_stale_stdin(self):
        # This session last heard 40%, but other sessions pushed the shared quota to 56%.
        got = sl.merge_usage(stdin_rl(five=(40, NOW + HOUR)), live(five=(56, NOW + HOUR - 0.4)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 56)

    def test_fresher_stdin_wins_over_cached_live(self):
        # A reply landed after the cache was written.
        got = sl.merge_usage(stdin_rl(five=(58, NOW + HOUR)), live(five=(56, NOW + HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 58)

    def test_new_window_beats_old_window(self):
        # stdin still describes the window that already reset; live has the new one.
        got = sl.merge_usage(stdin_rl(five=(55, NOW - 60)), live(five=(3, NOW + 5 * HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 3)

    def test_expired_window_with_no_replacement_is_unknown(self):
        # The window reset and no newer reading exists: show "--", not a made-up 0%.
        got = sl.merge_usage(stdin_rl(five=(55, NOW - 60)), None, NOW)
        self.assertIsNone(got["five_hour"])

    def test_expired_window_renders_as_unknown(self):
        usage = sl.merge_usage(stdin_rl(five=(55, NOW - 60), seven=(40, NOW + HOUR)), None, NOW)
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW))
        self.assertRegex(out, r"5h\s+--")
        self.assertNotRegex(out, r"5h ↔ 0%")

    def test_no_data_is_unknown_not_zero(self):
        got = sl.merge_usage({}, None, NOW)
        self.assertIsNone(got["five_hour"])
        self.assertIsNone(got["seven_day"])

    def test_windows_merge_independently(self):
        got = sl.merge_usage(stdin_rl(seven=(48, NOW + 5 * 24 * HOUR)), live(five=(12, NOW + HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 12)
        self.assertEqual(got["seven_day"]["pct"], 48)


class Render(unittest.TestCase):
    def test_fresh_session_shows_unknown_not_fake_zero(self):
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=None, now=NOW))
        self.assertNotIn("5h:0%", out)
        self.assertRegex(out, r"5h\s+--")
        self.assertRegex(out, r"7d\s+--")

    def test_shows_context_tokens_used_of_window_size(self):
        cw = {
            "context_window_size": 1_000_000,
            "used_percentage": 14,
            "current_usage": {"input_tokens": 2, "output_tokens": 148,
                              "cache_creation_input_tokens": 787, "cache_read_input_tokens": 134_488},
        }
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertRegex(out, r"ctx ↔ 14% \S+ 135k/1M")

    def test_context_tokens_zero_before_first_reply(self):
        cw = {"context_window_size": 200_000, "used_percentage": 0, "current_usage": None}
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertRegex(out, r"ctx ↔ 0% \S+ 0/200k")

    def test_ctx_label_takes_the_percentage_color_like_usage(self):
        raw = sl.format_statusline({"context_window": {"used_percentage": 43}}, now=NOW, show_usage=False)
        c = sl.color_pct(43)
        self.assertIn(f"{c}ctx{sl.RST}", raw)
        self.assertIn(f"{c}43%{sl.RST}", raw)

    def test_duration_is_labelled_as_session_time(self):
        data = {"cost": {"total_duration_ms": 5_580_000}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        self.assertIn("session 1h33m", out)

    def test_countdown_drops_zero_parts(self):
        self.assertEqual([sl.fmt_until(s) for s in (5 * 24 * HOUR, 5 * 24 * HOUR + 2 * HOUR, 3 * HOUR,
                                                    3 * HOUR + 5 * 60, 14 * 60)],
                         ["5d", "5d2h", "3h", "3h05m", "14m"])

    def test_cost_always_has_two_decimals(self):
        self.assertEqual([plain(sl.fmt_cost(c)) for c in (47.5, 0.37, 120)], ["$47.50", "$0.37", "$120.00"])

    def test_usage_colors_come_in_four_stages(self):
        green, yellow, orange, red = sl.fg(114), sl.fg(221), sl.fg(208), sl.fg(203)
        cases = {0: green, 49: green, 50: yellow, 69: yellow, 70: orange, 84: orange,
                 85: red, 99: red, 100: red}
        self.assertEqual({p: sl.color_pct(p) for p in cases}, cases)
        self.assertEqual(sl.RED, red)

    def test_shows_effort_level_after_model(self):
        data = {"model": {"display_name": "Opus"}, "effort": {"level": "xhigh"}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus · xhigh  │"), out)

    def test_effort_omitted_when_absent(self):
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus  │"), out)

    def test_token_counts_are_compact(self):
        self.assertEqual([sl.fmt_tokens(n) for n in (0, 950, 1_000, 12_345, 135_277, 200_000, 1_000_000, 1_250_000)],
                         ["0", "950", "1k", "12k", "135k", "200k", "1M", "1.2M"])

    def test_shows_live_percent_and_reset_countdown(self):
        usage = sl.merge_usage({}, live(five=(56, NOW + 14 * 60 + 5), seven=(48, NOW + 5 * 24 * HOUR + 2 * HOUR)), NOW)
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW))
        self.assertRegex(out, "5h ↔ 56% \\S+ 14m")
        self.assertRegex(out, "7d ↔ 48% \\S+ 5d2h")

    def test_usage_label_takes_the_percentage_color(self):
        usage = sl.merge_usage({}, live(five=(56, NOW + HOUR)), NOW)
        raw = sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW)
        c = sl.color_pct(56)
        self.assertIn(f"{c}5h{sl.RST}", raw)
        self.assertIn(f"{c}56%{sl.RST}", raw)


class VersionStatus(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(sl.version_status("2.1.296", ["2.1.295", "2.1.296"], "2.1.296"), ("ok", None))

    def test_newer_build_installed_needs_restart(self):
        self.assertEqual(sl.version_status("2.1.290", ["2.1.290", "2.1.296"], "2.1.296"), ("restart", "2.1.296"))

    def test_newer_release_published(self):
        self.assertEqual(sl.version_status("2.1.290", ["2.1.290"], "2.1.296"), ("update", "2.1.296"))

    def test_compares_numerically_not_as_text(self):
        self.assertEqual(sl.version_status("2.1.99", [], "2.1.100"), ("update", "2.1.100"))

    def test_unknown_when_nothing_to_compare(self):
        self.assertEqual(sl.version_status("2.1.296", [], None), ("unknown", None))


class PluginsNeedingReload(unittest.TestCase):
    INSTALLED = {"plugins": {
        "a@m": [{"lastUpdated": "2026-10-09T16:00:00Z"}],
        "b@m": [{"lastUpdated": "2026-10-09T18:00:00Z"}],
        "off@m": [{"lastUpdated": "2026-10-09T18:00:00Z"}],
    }}

    def test_enabled_plugins_updated_after_load(self):
        loaded = sl._epoch("2026-10-09T17:00:00Z")
        self.assertEqual(sl.plugins_needing_reload(self.INSTALLED, {"a@m", "b@m"}, loaded), ["b@m"])

    def test_none_after_reload(self):
        loaded = sl._epoch("2026-10-09T19:00:00Z")
        self.assertEqual(sl.plugins_needing_reload(self.INSTALLED, {"a@m", "b@m"}, loaded), [])


class McpStatus(unittest.TestCase):
    SID = "s1"

    def ev(self, msg, sid="s1", key="debug", ts="2026-10-09T19:00:00Z"):
        return {key: msg, "sessionId": sid, "timestamp": ts}

    def test_last_connection_event_for_this_session_wins(self):
        logs = {
            "plugin-discord-discord": [self.ev("Connection failed (CONNECTION_CLOSED): Connection closed", key="error")],
            "claude-ai-Gmail": [self.ev("Successfully connected (transport: claudeai-proxy) in 4ms")],
            "plugin-resend-resend": [
                self.ev("Connection failed (400): bad request", key="error", ts="2026-10-09T19:00:00Z"),
                self.ev("Successfully connected", ts="2026-10-09T19:05:00Z"),
            ],
            "plugin-telegram-telegram": [self.ev("Connection failed", sid="other", key="error")],
        }
        ok, failed = sl.mcp_status(logs, self.SID)
        self.assertEqual(sorted(ok), ["Gmail", "resend"])
        self.assertEqual(failed, ["discord"])

    def test_tool_errors_after_connecting_do_not_count(self):
        logs = {"plugin-x-x": [self.ev("Successfully connected"),
                               self.ev("Tool call failed: timeout", key="error", ts="2026-10-09T19:09:00Z")]}
        self.assertEqual(sl.mcp_status(logs, self.SID), (["x"], []))


class SkillCount(unittest.TestCase):
    def test_counts_enabled_plugin_skills_commands_and_user_skills(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def skill(d):
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text("x")

            listed = root / "listed"  # manifest names its skills: only those load
            (listed / ".claude-plugin").mkdir(parents=True)
            (listed / ".claude-plugin/plugin.json").write_text(json.dumps({"skills": ["./skills/a/one", "./skills/a/two"]}))
            for n in ("one", "two", "unlisted"):
                skill(listed / "skills/a" / n)
            plain_p = root / "plain"  # no list: each skills/<name>/SKILL.md, plus commands
            skill(plain_p / "skills/x")
            (plain_p / "commands").mkdir()
            (plain_p / "commands/do.md").write_text("x")
            off = root / "off"
            skill(off / "skills/y")
            installed = {"plugins": {"listed@m": [{"installPath": str(listed)}],
                                     "plain@m": [{"installPath": str(plain_p)}],
                                     "off@m": [{"installPath": str(off)}]}}
            user = root / "claude"
            skill(user / "skills/mine")
            project = root / "proj"
            skill(project / ".claude/skills/local")
            n = sl.count_skills(installed, {"listed@m", "plain@m"}, user, project)
            self.assertEqual(n, 2 + 2 + 1 + 1)  # listed 2, plain skill + command, user 1, project 1


class McpLogFiles(unittest.TestCase):
    def test_finds_this_session_behind_newer_files_of_other_sessions(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as root:
            d = pathlib.Path(root) / "mcp-logs-plugin-x-x"
            d.mkdir()
            ours = d / "2026-10-09T19-00-00-000Z.jsonl"
            ours.write_text(json.dumps({"debug": "Successfully connected", "sessionId": "me",
                                        "timestamp": "2026-10-09T19:00:01Z"}) + "\n")
            for i in range(5):
                f = d / f"2026-10-09T20-0{i}-00-000Z.jsonl"
                f.write_text(json.dumps({"debug": "Successfully connected", "sessionId": f"other{i}"}) + "\n")
            logs = sl.read_mcp_logs(pathlib.Path(root), since=os.path.getmtime(ours) - 60)
            self.assertEqual(sl.mcp_status(logs, "me"), (["x"], []))

    def test_skips_files_last_written_before_the_session(self):
        import json, os, tempfile, time
        with tempfile.TemporaryDirectory() as root:
            d = pathlib.Path(root) / "mcp-logs-plugin-x-x"
            d.mkdir()
            old = d / "2026-10-01T10-00-00-000Z.jsonl"
            old.write_text(json.dumps({"debug": "Successfully connected", "sessionId": "me"}) + "\n")
            os.utime(old, (time.time() - 86400, time.time() - 86400))
            logs = sl.read_mcp_logs(pathlib.Path(root), since=time.time() - 3600)
            self.assertEqual(logs, {"plugin-x-x": []})


class McpNames(unittest.TestCase):
    def test_display_names(self):
        known = {"chrome-devtools-mcp", "discord"}
        got = [sl._mcp_name(n, known) for n in
               ("plugin-discord-discord", "plugin-chrome-devtools-mcp-chrome-devtools", "claude-ai-Google-Drive", "bigquery")]
        self.assertEqual(got, ["discord", "chrome-devtools", "Google-Drive", "bigquery"])


class HealthLine(unittest.TestCase):
    def test_all_good(self):
        out = plain(sl.format_health("2.1.296", ("ok", None), [], (["a"] * 16, [])))
        self.assertEqual(out, "Claude Code 2.1.296 ✓  │  plugins ✓  │  mcp 16 connected")

    def test_mcp_none_connected_some_failed(self):
        out = plain(sl.format_health(None, ("unknown", None), None, ([], ["discord", "a", "b"])))
        self.assertEqual(out, "mcp ▲ discord +2 failed")

    def test_mcp_one_failed(self):
        out = plain(sl.format_health(None, ("unknown", None), None, (["a"] * 13, ["discord"])))
        self.assertEqual(out, "mcp 13 connected · ▲ discord failed")

    def test_skill_count_follows_plugins(self):
        out = plain(sl.format_health("2.1.296", ("ok", None), [], None, skills=69))
        self.assertEqual(out, "Claude Code 2.1.296 ✓  │  plugins ✓  │  skills 69")

    def test_needs_attention(self):
        out = plain(sl.format_health("2.1.290", ("restart", "2.1.296"), ["p1", "p2", "p3"],
                                     (["a"] * 15, ["discord", "resend"])))
        self.assertEqual(out, "Claude Code 2.1.290 ↑ restart for 2.1.296"
                              "  │  plugins ↻ 3 updated · run /reload-plugins"
                              "  │  mcp 15 connected · ▲ discord +1 failed")

    def test_update_available_says_so(self):
        out = plain(sl.format_health("2.1.290", ("update", "2.1.296"), None, None))
        self.assertEqual(out, "Claude Code 2.1.290 ↑ 2.1.296 available")

    def test_sections_without_data_are_hidden(self):
        self.assertEqual(plain(sl.format_health(None, ("unknown", None), None, None)), "")


class Subagents(unittest.TestCase):
    def asst(self, stop, model="claude-haiku-5-5", effort="medium", cache_read=47_743):
        return {"type": "assistant", "effort": effort, "message": {
            "model": model, "stop_reason": stop,
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1935,
                      "cache_read_input_tokens": cache_read, "output_tokens": 9}}}

    def test_running_agent_reports_model_effort_tokens(self):
        entries = [{"type": "user"}, self.asst("tool_use"), {"type": "user"}, {"type": "attachment"}]
        self.assertEqual(sl.subagent_state(entries),
                         {"model": "claude-haiku-5-5", "effort": "medium", "tokens": 49_680, "running": True})

    def test_finished_agent(self):
        entries = [{"type": "user"}, self.asst("tool_use"), {"type": "user"}, self.asst("end_turn")]
        self.assertFalse(sl.subagent_state(entries)["running"])

    def test_agent_with_no_reply_yet_is_running(self):
        self.assertEqual(sl.subagent_state([{"type": "user"}]),
                         {"model": None, "effort": None, "tokens": 0, "running": True})

    def test_model_display_names(self):
        self.assertEqual([sl.fmt_model_id(m) for m in ("claude-haiku-5-5", "claude-opus-5-5[1m]", "claude-sonnet-4-6", "custom-x")],
                         ["Haiku 5.5", "Opus 5.5", "Sonnet 4.6", "custom-x"])

    def test_agents_line(self):
        agents = [
            {"type": "claude-code-guide", "model": "claude-haiku-5-5", "effort": "medium", "tokens": 49_680},
            {"type": "Explore", "model": "claude-sonnet-5-5", "effort": None, "tokens": 120_000},
            {"type": "general-purpose", "model": None, "effort": None, "tokens": 0},
            {"type": "Plan", "model": "claude-opus-5-5", "effort": "high", "tokens": 5_000},
        ]
        self.assertEqual(plain(sl.format_agents(agents)),
                         "agents 4  │  claude-code-guide Haiku 5.5 · medium 50k"
                         "  │  Explore Sonnet 5.5 120k  │  general-purpose  │  +1")

    def test_agent_name_and_model_use_model_color(self):
        out = sl.format_agents([{"type": "Explore", "model": "claude-sonnet-5-5", "effort": None, "tokens": 1}])
        c = sl.model_color("claude-sonnet-5-5")
        self.assertIn(f"{c}Explore", out)
        self.assertIn(f"{c}Sonnet 5.5", out)

    def test_no_running_agents_no_line(self):
        self.assertEqual(sl.format_agents([]), "")


class ModelColors(unittest.TestCase):
    def test_each_family_has_its_own_color(self):
        fams = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
        colors = [sl.model_color(m) for m in fams]
        self.assertEqual(len(set(colors)), 4)
        self.assertEqual(sl.model_color("claude-opus-4-6"), sl.model_color("claude-opus-5-5[1m]"))
        self.assertEqual(sl.model_color("something-else"), sl.MODEL)
        self.assertEqual(sl.model_color(None), sl.MODEL)

    def test_main_model_name_uses_its_color(self):
        data = {"model": {"id": "claude-haiku-5-5", "display_name": "Haiku 5.5"}}
        out = sl.format_statusline(data, now=NOW, show_usage=False)
        self.assertTrue(out.startswith(f"{sl.model_color('claude-haiku-5-5')}Haiku 5.5"), repr(out[:40]))


class Layout(unittest.TestCase):
    DATA = {"model": {"display_name": "Opus"}, "workspace": {"current_dir": "C:/work/my-app"}}

    def test_workspace_can_move_off_line_one(self):
        line1 = plain(sl.format_statusline(self.DATA, now=NOW, show_usage=False, workspace=False))
        self.assertNotIn("my-app", line1)
        self.assertTrue(plain(sl.workspace_line(self.DATA)).startswith("my-app"))

    def test_bars_use_full_blocks(self):
        out = plain(sl.bar(50))
        self.assertEqual(out, "█" * 8)

    def test_plain_bar_lights_one_block_for_any_usage(self):
        self.assertEqual(sl.bar(2), f"{sl.color_pct(2)}█{sl.RST}{sl.BAR_TRACK}{'█' * 7}{sl.RST}")
        self.assertEqual(sl.bar(0), f"{sl.color_pct(0)}{sl.RST}{sl.BAR_TRACK}{'█' * 8}{sl.RST}")

    def test_long_branch_is_not_capped_when_there_is_room(self):
        branch = "live-usage-and-redesign-of-the-meters"
        out = plain(sl.where_item("claude-code-statusline", (branch, []), width=116))
        self.assertEqual(out, f"claude-code-statusline  ›  {branch}")

    def test_project_and_branch_shrink_from_the_middle_to_fit(self):
        proj, branch = "claude-code-statusline", "live-usage-and-redesign-of-the-meters"
        out = plain(sl.where_item(proj, (branch, ["+1"]), width=40))
        self.assertLessEqual(len(out), 40, out)
        self.assertGreaterEqual(len(out), 38, out)  # shortened just enough
        p, b = out.split("  ›  ")
        self.assertIn("…", p + b)
        self.assertTrue(b.endswith(" +1"), out)
        for short, full in ((p, proj), (b[:-3], branch)):
            if "…" in short:
                head, tail = short.split("…")
                self.assertTrue(full.startswith(head) and full.endswith(tail), short)

    def test_middle_ellipsis(self):
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 30), "claude-code-statusline")
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 19), "claude-co…tatusline")
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 20), "claude-co…statusline")


class Flow(unittest.TestCase):
    def test_items_move_whole_to_the_next_line(self):
        items = ["aaaa", "bbbb", "cccc", "dd"]
        # "aaaa  │  bbbb" is 13 wide; adding "  │  cccc" would make 22.
        self.assertEqual([plain(l) for l in sl.flow(items, 20)],
                         ["aaaa  │  bbbb", "cccc  │  dd"])

    def test_an_item_wider_than_the_line_gets_a_line_of_its_own(self):
        self.assertEqual([plain(l) for l in sl.flow(["x" * 30, "y"], 20)], ["x" * 30, "y"])

    def test_no_lonely_last_item_when_one_can_move_down(self):
        items = ["aa", "bb", "cc", "dddd"]
        # Greedy gives "aa  │  bb  │  cc" then "dddd" alone; "cc" moves down to keep it company.
        self.assertEqual([plain(l) for l in sl.flow(items, 20)], ["aa  │  bb", "cc  │  dddd"])

    def test_balance_never_overflows_the_last_line(self):
        items = ["aa", "bb", "cc", "d" * 19]
        self.assertEqual([plain(l) for l in sl.flow(items, 20)], ["aa  │  bb  │  cc", "d" * 19])

    def test_width_ignores_color_codes(self):
        self.assertEqual(sl.visible_width(f"{sl.GREEN}abc{sl.RST}"), 3)

    def test_rendered_lines_fit_the_window(self):
        import io, json, os
        from contextlib import redirect_stdout
        from unittest import mock
        data = {"model": {"display_name": "Opus 5.5", "id": "claude-opus-5-5"}, "effort": {"level": "high"},
                "context_window": {"used_percentage": 11, "context_window_size": 1_000_000,
                                   "current_usage": {"input_tokens": 135_000}},
                "rate_limits": {"five_hour": {"used_percentage": 14, "resets_at": 9e9},
                                "seven_day": {"used_percentage": 54, "resets_at": 9e9}},
                "cost": {"total_cost_usd": 47.5, "total_duration_ms": 720_000},
                "workspace": {"current_dir": os.getcwd()}}
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), \
                mock.patch.object(sys, "argv", ["statusline.py", "--icons", "nerd"]), \
                mock.patch.dict(os.environ, {"COLUMNS": "120", "STATUSLINE_LIVE_USAGE": "0"}), \
                redirect_stdout(buf), self.assertRaises(SystemExit):
            sl.main()
        sl.set_icons("none")
        lines = buf.getvalue().rstrip("\n").split("\n")
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(sl.visible_width(line), 120 - sl.EDGE, plain(line))
        self.assertIn("47.50", plain(buf.getvalue()))


class SlantedBars(unittest.TestCase):
    SEG = ""  # lower-right + upper-left triangle: one slanted segment

    def tearDown(self):
        sl.set_icons("none")

    def test_nerd_bar_is_five_slanted_segments(self):
        sl.set_icons("nerd")
        self.assertEqual(plain(sl.bar(60)), self.SEG * 5)

    def test_filled_segments_take_the_ramp_color_and_the_rest_the_track(self):
        sl.set_icons("nerd")
        self.assertEqual(sl.bar(60), f"{sl.color_pct(60)}{self.SEG * 3}{sl.RST}{sl.BAR_TRACK}{self.SEG * 2}{sl.RST}")

    def test_any_usage_shows_at_least_one_segment(self):
        sl.set_icons("nerd")
        self.assertIn(f"{sl.color_pct(3)}{self.SEG}{sl.RST}{sl.BAR_TRACK}{self.SEG * 4}", sl.bar(3))


class Icons(unittest.TestCase):
    DATA = {"model": {"display_name": "Opus"}, "cost": {"total_cost_usd": 2.39},
            "context_window": {"used_percentage": 14}}

    def tearDown(self):
        sl.set_icons("none")

    def test_plain_by_default(self):
        out = plain(sl.format_statusline(self.DATA, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus  │  ctx"), out)
        self.assertIn("$2.39", out)

    def test_nerd_icons(self):
        sl.set_icons("nerd")
        out = plain(sl.format_statusline(self.DATA, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("󱙺 Opus  │  󰠰 ctx"), out)
        self.assertIn("󰇁 2.39", out)
        health = plain(sl.format_health("2.1.296", ("ok", None), [], (["a"], ["discord"])))
        self.assertEqual(health, "󰞷 Claude Code 2.1.296 󰗠  │  󰐱 plugins 󰗠"
                                 "  │  󰒍 mcp 1 connected · 󰀦 discord failed")

    def test_plugin_reload_icon_differs_from_version_arrow(self):
        sl.set_icons("nerd")
        out = plain(sl.format_health(None, ("unknown", None), ["p1", "p2"], None))
        self.assertEqual(out, "󰐱 plugins \U000F0453 2 updated · run /reload-plugins")
        self.assertNotEqual(sl.ICON["reload"], sl.ICON["up"])

    def test_cost_is_bold_bright_green(self):
        out = sl.format_statusline(self.DATA, now=NOW, show_usage=False)
        self.assertIn("[1;38;5;46m$2.39", out)


if __name__ == "__main__":
    unittest.main()
