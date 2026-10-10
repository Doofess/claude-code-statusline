"""Tests for statusline.py. Run: python -m unittest test_statusline"""

import re
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

    def test_expired_window_with_no_replacement_reads_zero(self):
        # The window reset and nothing has been used since, so 0% is the truth.
        got = sl.merge_usage(stdin_rl(five=(55, NOW - 60)), None, NOW)
        self.assertEqual(got["five_hour"]["pct"], 0)

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
        self.assertRegex(out, r"ctx \S+ 14% 135k/1M")

    def test_context_tokens_zero_before_first_reply(self):
        cw = {"context_window_size": 200_000, "used_percentage": 0, "current_usage": None}
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertRegex(out, r"ctx \S+ 0% 0/200k")

    def test_shows_effort_level_after_model(self):
        data = {"model": {"display_name": "Opus"}, "effort": {"level": "xhigh"}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus · xhigh │"), out)

    def test_effort_omitted_when_absent(self):
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus │"), out)

    def test_token_counts_are_compact(self):
        self.assertEqual([sl.fmt_tokens(n) for n in (0, 950, 1_000, 12_345, 135_277, 200_000, 1_000_000, 1_250_000)],
                         ["0", "950", "1k", "12k", "135k", "200k", "1M", "1.2M"])

    def test_shows_live_percent_and_reset_countdown(self):
        usage = sl.merge_usage({}, live(five=(56, NOW + 14 * 60 + 5), seven=(48, NOW + 5 * 24 * HOUR + 2 * HOUR)), NOW)
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW))
        self.assertRegex(out, r"5h \S+ 56% 14m")
        self.assertRegex(out, r"7d \S+ 48% 5d2h")


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


class McpNames(unittest.TestCase):
    def test_display_names(self):
        known = {"chrome-devtools-mcp", "discord"}
        got = [sl._mcp_name(n, known) for n in
               ("plugin-discord-discord", "plugin-chrome-devtools-mcp-chrome-devtools", "claude-ai-Google-Drive", "bigquery")]
        self.assertEqual(got, ["discord", "chrome-devtools", "Google-Drive", "bigquery"])


class HealthLine(unittest.TestCase):
    def test_all_good(self):
        out = plain(sl.format_health("2.1.296", ("ok", None), [], (["a"] * 16, [])))
        self.assertEqual(out, "cc 2.1.296 ✓ │ plugins ✓ │ mcp 16")

    def test_needs_attention(self):
        out = plain(sl.format_health("2.1.290", ("restart", "2.1.296"), ["p1", "p2", "p3"],
                                     (["a"] * 15, ["discord", "resend"])))
        self.assertEqual(out, "cc 2.1.290 ↑ 2.1.296 restart │ plugins ↑ 3 /reload-plugins"
                              " │ mcp 15 ▲ discord +1")

    def test_sections_without_data_are_hidden(self):
        self.assertEqual(plain(sl.format_health(None, ("unknown", None), None, None)), "")


if __name__ == "__main__":
    unittest.main()
