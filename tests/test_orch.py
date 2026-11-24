import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "scripts")
CATALOG_PATH = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "catalog.yaml")
FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures", "agy-usage-sample.json")

sys.path.insert(0, SCRIPTS_DIR)
import orch  # noqa: E402


def _assert_none_due(tc, text):
    """`followups` with nothing due prints only its closing `action:` line."""
    lines = text.strip().splitlines()
    tc.assertEqual(len(lines), 1, text)
    tc.assertTrue(lines[0].startswith("action: no follow-up is due"), text)


def _Args(**kw):
    """CLI-argument stand-in. These tests assert the detailed (`--full`) output; compact output is covered in test_output_contract.py."""
    kw.setdefault("full", True)
    return mock.Mock(**kw)


class TestAntigravityGroupQuota(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)
        self.catalog = orch.load_file(CATALOG_PATH)
        self.now = orch.get_now("2026-11-20T21:00:00Z")

    def test_parse_fixture_groups_and_model_mapping(self):
        result, err = orch.parse_antigravity_usage(self.fixture, self.now, catalog=self.catalog)
        self.assertIsNone(err)
        groups = {g["name"]: g for g in result["groups"]}
        self.assertEqual(
            set(groups.keys()),
            {"Gemini Models", "Claude and GPT models", "Gemini 4 Argon"},
        )

        # Gemini Models: weekly 47.36% remaining -> 52.6% used; 5h 75.53% remaining -> 24.5% used.
        # Most constraining active bucket is weekly (7d).
        gemini = groups["Gemini Models"]
        self.assertEqual(gemini["used_percent"], 52.6)
        self.assertEqual(gemini["window"], "7d")
        self.assertEqual(gemini["resets_at"], "2026-11-26T15:58:04+00:00")
        self.assertEqual(
            gemini["models"],
            ["gemini-4-neon", "gemini-4-hydrogen", "gemini-pro-orchestrator-neon", "gemini-3-8-flash"],
        )

        # Claude and GPT models: weekly 0% remaining -> 100% used; 5h disabled -> skipped.
        cg = groups["Claude and GPT models"]
        self.assertEqual(cg["used_percent"], 100)
        self.assertEqual(cg["window"], "7d")
        self.assertEqual(cg["resets_at"], "2026-11-27T15:59:36+00:00")
        self.assertEqual(
            cg["models"],
            ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-oss-120b"],
        )

        # Gemini 4 Argon: standalone group matched by group name -> gemini-4-argon, 0% used.
        argon = groups["Gemini 4 Argon"]
        self.assertEqual(argon["used_percent"], 0)
        self.assertEqual(argon["window"], "7d")
        self.assertEqual(argon["resets_at"], "2026-11-27T21:23:09+00:00")
        self.assertEqual(argon["models"], ["gemini-4-argon"])

        # All fixture model names (including "Orchestrator (Neon)") map cleanly to catalog ids.
        self.assertEqual(result["unmatched"], [])

    def test_disabled_5h_bucket_ignored_even_when_higher_usage(self):
        data = copy.deepcopy(self.fixture)
        # Set weekly to 60% remaining (40% used), keep 5h disabled with 0.01 remaining (99% used).
        cg_buckets = data["command"]["data"]["groups"][1]["buckets"]
        cg_buckets[0]["remaining_fraction"] = 0.6
        cg_buckets[1]["disabled"] = True
        cg_buckets[1]["remaining_fraction"] = 0.01

        result, err = orch.parse_antigravity_usage(data, self.now, catalog=self.catalog)
        self.assertIsNone(err)
        cg = next(g for g in result["groups"] if g["name"] == "Claude and GPT models")
        self.assertEqual(cg["used_percent"], 40)
        self.assertEqual(cg["window"], "7d")

    def test_expired_bucket_reset_skipped(self):
        # At 2026-11-21T02:00:00Z, gemini-5h (2026-11-21T01:46:21Z) has expired, weekly still active.
        data = copy.deepcopy(self.fixture)
        # Make 5h bucket 99% used (would normally win), but expired relative to later_now.
        data["command"]["data"]["groups"][0]["buckets"][1]["remaining_fraction"] = 0.01
        later_now = orch.get_now("2026-11-21T02:00:00Z")

        result, err = orch.parse_antigravity_usage(data, later_now, catalog=self.catalog)
        self.assertIsNone(err)
        gemini = next(g for g in result["groups"] if g["name"] == "Gemini Models")
        self.assertEqual(gemini["used_percent"], 52.6)
        self.assertEqual(gemini["window"], "7d")

        # Past ALL reset times -> every group reports 0% / reset.
        far_future = orch.get_now("2026-12-01T00:00:00Z")
        result_future, err = orch.parse_antigravity_usage(self.fixture, far_future, catalog=self.catalog)
        self.assertIsNone(err)
        for g in result_future["groups"]:
            self.assertEqual(g["used_percent"], 0)
            self.assertEqual(g["window"], "reset")
            self.assertIsNone(g["resets_at"])

    def test_probe_persists_groups_and_best_provider_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )

            fake_proc = subprocess.CompletedProcess(
                args=["agy", "-p", "/usage", "--output-format", "json"],
                returncode=0,
                stdout=json.dumps(self.fixture),
                stderr="",
            )
            args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                low_threshold=None,
            )
            buf = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf):
                rc = orch.probe(args)

            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("antigravity: ok (2/3 groups usable)", out)
            self.assertIn("group Gemini Models: ok (52.6% of 7d", out)
            self.assertIn("group Claude and GPT models: exhausted (100% of 7d", out)
            self.assertIn("group Gemini 4 Argon: ok (0% of 7d", out)
            self.assertNotIn("unmatched model names", out)

            # Verify persisted YAML round-trips cleanly and passes orch.py check.
            loaded = orch.load_file(assets_path)
            ag = loaded["providers"][0]
            self.assertEqual(ag["status"], "ok")
            self.assertEqual(ag["policy"], "spend-first")
            self.assertEqual(len(ag["groups"]), 3)
            by_name = {g["name"]: g for g in ag["groups"]}
            self.assertEqual(by_name["Claude and GPT models"]["status"], "exhausted")
            self.assertEqual(by_name["Gemini Models"]["status"], "ok")
            self.assertEqual(by_name["Gemini 4 Argon"]["status"], "ok")
            self.assertIn("gemini-pro-orchestrator-neon", by_name["Gemini Models"]["models"])

            check_args = _Args(assets=assets_path, catalog=CATALOG_PATH)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.check(check_args), 0)

    def test_select_excludes_exhausted_group_and_picks_gemini_4_neon(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                    "  - id: claude\n"
                    "    policy: preserve\n"
                    "    status: ok\n"
                )

            fake_proc = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr=""
            )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                low_threshold=None,
            )
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(io.StringIO()):
                orch.probe(probe_args)

            # select --complexity hard --risk high (T4 >= 57) with antigravity models + claude fallback
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="high",
                available="antigravity:claude-opus-5-5,antigravity:gemini-4-neon,antigravity:gemini-4-argon,claude:claude-opus-5-5",
                ui=False,
                fail_on_low=False,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("implementer: antigravity / gemini-4-neon @ high", out)
            self.assertIn("reviewer (T4, different family): claude / claude-opus-5-5 @ xhigh", out)
            self.assertIn("antigravity:claude-opus-5-5: group Claude and GPT models exhausted", out)

            # Even with --ui (which prefers Anthropic), antigravity:claude-opus-5-5 is excluded.
            sel_ui = _Args(**{**sel_args.__dict__, "ui": True, "available": "antigravity:claude-opus-5-5,antigravity:gemini-4-neon"})
            buf_ui = io.StringIO()
            with contextlib.redirect_stdout(buf_ui):
                rc_ui = orch.select(sel_ui)
            self.assertEqual(rc_ui, 0)
            self.assertIn("implementer: antigravity / gemini-4-neon @ high", buf_ui.getvalue())

            # After the Claude/GPT group reset passes, antigravity:claude-opus-5-5 becomes eligible again.
            sel_after = _Args(
                **{
                    **sel_args.__dict__,
                    "now": "2026-11-28T00:00:00Z",
                    "ui": True,
                    "available": "antigravity:claude-opus-5-5,antigravity:gemini-4-neon",
                }
            )
            buf_after = io.StringIO()
            with contextlib.redirect_stdout(buf_after):
                rc_after = orch.select(sel_after)
            self.assertEqual(rc_after, 0)
            self.assertIn("implementer: antigravity / claude-opus-5-5 @ xhigh", buf_after.getvalue())

    def test_low_status_applies_per_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: normal\n"
                    "    status: ok\n"
                    "    groups:\n"
                    '      - name: "Gemini Models"\n'
                    "        models: [gemini-4-neon, gemini-4-hydrogen]\n"
                    "        status: low\n"
                    "        used_percent: 90\n"
                    "        window: 7d\n"
                    "        resets_at: 2026-11-26T15:58:04+00:00\n"
                    '      - name: "Gemini 4 Argon"\n'
                    "        models: [gemini-4-argon]\n"
                    "        status: ok\n"
                    "        used_percent: 10\n"
                    "        window: 7d\n"
                    "        resets_at: 2026-11-27T21:23:09+00:00\n"
                )

            # T3 (hard/low, >=50): gemini-4-argon (ok, score 51) beats gemini-4-neon (low, score 55).
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="low",
                available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                ui=False,
                fail_on_low=False,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            self.assertIn("implementer: antigravity / gemini-4-argon @ high", buf.getvalue())

            # With --fail-on-low, gemini-4-neon alone is blocked (exit 3), while gemini-4-argon succeeds (exit 0).
            sel_blocked = _Args(**{**sel_args.__dict__, "available": "antigravity:gemini-4-neon", "fail_on_low": True})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.select(sel_blocked), 3)

            sel_ok = _Args(**{**sel_args.__dict__, "available": "antigravity:gemini-4-argon", "fail_on_low": True})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.select(sel_ok), 0)

    def test_low_group_penalized_below_ok_provider_even_with_higher_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    groups:\n"
                    '      - name: "Gemini Models"\n'
                    "        models: [gemini-4-neon]\n"
                    "        status: low\n"
                    "        used_percent: 90\n"
                    "        window: 7d\n"
                    "        resets_at: 2026-11-26T15:58:04+00:00\n"
                    "  - id: codex\n"
                    "    policy: normal\n"
                    "    status: ok\n"
                )
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="low",
                available="antigravity:gemini-4-neon,codex:gpt-6-2-sol",
                ui=False,
                fail_on_low=False,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            self.assertIn("implementer: codex / gpt-6-2-sol @ medium", buf.getvalue())

    def test_all_groups_exhausted_sets_provider_exhausted_and_reprobe_overwrites(self):
        data = copy.deepcopy(self.fixture)
        for g in data["command"]["data"]["groups"]:
            for b in g["buckets"]:
                if not b.get("disabled"):
                    b["remaining_fraction"] = 0

        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )

            fake_all_exhausted = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(data), stderr=""
            )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                low_threshold=None,
            )
            buf = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_all_exhausted
            ), contextlib.redirect_stdout(buf):
                rc = orch.probe(probe_args)
            self.assertEqual(rc, 4)
            self.assertIn("antigravity: exhausted (0/3 groups usable", buf.getvalue())

            loaded = orch.load_file(assets_path)
            ag = loaded["providers"][0]
            self.assertEqual(ag["status"], "exhausted")
            # Earliest reset among the exhausted groups (gemini-5h is 2026-11-21T01:46:21+00:00,
            # while Gemini Models most constraining tie-break picks weekly 2026-11-26 since both 100% -> latest reset).
            self.assertEqual(ag["exhausted_until"], "2026-11-26T15:58:04+00:00")

            # Re-probing with the normal fixture replaces the groups block cleanly and clears exhaustion.
            fake_ok = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr=""
            )
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_ok
            ), contextlib.redirect_stdout(io.StringIO()):
                rc2 = orch.probe(probe_args)
            self.assertEqual(rc2, 0)
            loaded2 = orch.load_file(assets_path)
            ag2 = loaded2["providers"][0]
            self.assertEqual(ag2["status"], "ok")
            self.assertNotIn("exhausted_until", ag2)
            self.assertEqual(len(ag2["groups"]), 3)

    def test_provider_without_groups_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: claude\n"
                    "    policy: normal\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-25T00:00:00+00:00\n"
                    "  - id: codex\n"
                    "    policy: normal\n"
                    "    status: ok\n"
                )
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="low",
                available="claude:claude-opus-5-5,codex:gpt-6-2-sol",
                ui=False,
                fail_on_low=False,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("implementer: codex / gpt-6-2-sol @ medium", out)
            self.assertIn("claude:claude-opus-5-5: provider exhausted", out)


class TestUiAndPreferredFallback(unittest.TestCase):
    def _write_anthropic_exhausted_assets(self, path, ui_fallback=None):
        pref_line = f"  ui_fallback: {ui_fallback}\n" if ui_fallback else ""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "# user comment preserved\n"
                "version: 1\n"
                "updated: 2026-11-20T09:00:00+00:00\n"
                "preferences:\n"
                "  pay_per_use: never\n"
                f"{pref_line}"
                "providers:\n"
                "  - id: claude\n"
                "    policy: preserve\n"
                "    status: exhausted\n"
                "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: antigravity\n"
                "    policy: spend-first\n"
                "    status: ok\n"
                "    groups:\n"
                '      - name: "Gemini Models"\n'
                "        models: [gemini-4-neon, gemini-4-hydrogen]\n"
                "        status: ok\n"
                "        used_percent: 30\n"
                "        window: 7d\n"
                "        resets_at: 2026-11-26T15:58:04+00:00\n"
                '      - name: "Claude and GPT models"\n'
                "        models: [claude-opus-5-5, claude-sonnet-5-5]\n"
                "        status: exhausted\n"
                "        used_percent: 100\n"
                "        window: 7d\n"
                "        resets_at: 2026-11-27T15:59:36+00:00\n"
                "  - id: codex\n"
                "    policy: normal\n"
                "    status: ok\n"
            )

    def test_ui_fallback_proceed_marks_provisional_and_records_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_anthropic_exhausted_assets(assets_path)  # default is proceed

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="claude:claude-sonnet-5-5,antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon,codex:gpt-6-2-sol",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="PR #42",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("[provisional]", out)
            self.assertIn("2026-11-23T08:00:00+00:00", out)
            self.assertIn("implementer: antigravity / gemini-4-neon @ low", out)

            loaded = orch.load_file(assets_path)
            followups = loaded.get("followups") or []
            self.assertEqual(len(followups), 1)
            self.assertEqual(
                followups[0],
                {
                    "kind": "ui-review",
                    "ref": "PR #42",
                    "needs": "ui-ux-review",
                    "after": "2026-11-23T08:00:00+00:00",
                },
            )
            # Comment preserved
            with open(assets_path, encoding="utf-8") as fh:
                self.assertIn("# user comment preserved", fh.read())

            # Re-running with same ref does not duplicate
            with contextlib.redirect_stdout(io.StringIO()):
                orch.select(sel_args)
            self.assertEqual(len(orch.load_file(assets_path).get("followups") or []), 1)

    def test_ui_fallback_defer_reports_earliest_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_anthropic_exhausted_assets(assets_path, ui_fallback="defer")

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="claude:claude-sonnet-5-5,antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="PR #43",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 3)
            out = buf.getvalue()
            self.assertIn("DEFER", out)
            self.assertIn("2026-11-23T08:00:00+00:00", out)
            self.assertFalse(orch.load_file(assets_path).get("followups"))

    def test_ui_fallback_ask_prompts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_anthropic_exhausted_assets(assets_path, ui_fallback="ask")

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="claude:claude-sonnet-5-5,antigravity:gemini-4-neon",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="PR #44",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 3)
            out = buf.getvalue()
            self.assertIn("ASK", out)
            self.assertIn("2026-11-23T08:00:00+00:00", out)
            self.assertIn("antigravity / gemini-4-neon @ low", out)
            self.assertFalse(orch.load_file(assets_path).get("followups"))

    def test_generic_prefer_family_applies_same_fallback_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  ui_fallback: defer\n"
                    "providers:\n"
                    "  - id: codex\n"
                    "    policy: normal\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-22T12:00:00+00:00\n"
                    "  - id: antigravity\n"
                    "    policy: normal\n"
                    "    status: ok\n"
                )
            base = dict(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="codex:gpt-6-2-sol,antigravity:gemini-4-neon",
                ui=False,
                fail_on_low=False,
                prefer_family="openai",
                fallback=None,
                ref="branch-api",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(_Args(**base))
            self.assertEqual(rc, 3)
            self.assertIn("DEFER", buf.getvalue())
            self.assertIn("openai", buf.getvalue())
            self.assertIn("2026-11-22T12:00:00+00:00", buf.getvalue())

            # Override to proceed -> provisional + follow-up with needs: openai
            buf_p = io.StringIO()
            with contextlib.redirect_stdout(buf_p):
                rc_p = orch.select(_Args(**{**base, "fallback": "proceed"}))
            self.assertEqual(rc_p, 0)
            self.assertIn("[provisional]", buf_p.getvalue())
            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(fus[0]["needs"], "openai")
            self.assertEqual(fus[0]["ref"], "branch-api")

    def test_followup_reported_due_after_reset_and_cleared_with_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_anthropic_exhausted_assets(assets_path, ui_fallback="proceed")

            # Record follow-up while Anthropic is exhausted
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="claude:claude-sonnet-5-5,antigravity:gemini-4-neon",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="PR #99",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.select(sel_args), 0)

            # Before reset: followups --due reports none due
            fu_before = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-21T00:00:00Z",
                available=None,
                due=True,
                add=False,
                done=None,
            )
            buf_before = io.StringIO()
            with contextlib.redirect_stdout(buf_before):
                rc = orch.followups_cmd(fu_before)
            self.assertEqual(rc, 0)
            self.assertNotIn("PR #99", buf_before.getvalue())

            # After claude resets (2026-11-23T08:00:00+00:00) without discovery:
            # probe and followups --due report pending (not due) because no --available was passed.
            probe_after = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-23T09:00:00Z",
                provider=None,
                low_threshold=None,
                available=None,
            )
            buf_probe = io.StringIO()
            with mock.patch("orch._infer_probe", return_value=None), contextlib.redirect_stdout(buf_probe):
                orch.probe(probe_after)
            out_probe = buf_probe.getvalue()
            self.assertIn("pending: needs ui-ux-review", out_probe)
            self.assertNotIn("followup due", out_probe)
            self.assertIn("PR #99", out_probe)
            self.assertIn("claude", out_probe)

            fu_after_nodisc = _Args(**{**fu_before.__dict__, "now": "2026-11-23T09:00:00Z", "available": None})
            buf_after_nodisc = io.StringIO()
            with contextlib.redirect_stdout(buf_after_nodisc):
                orch.followups_cmd(fu_after_nodisc)
            self.assertIn("pending: needs ui-ux-review", buf_after_nodisc.getvalue())
            self.assertNotIn("followup due", buf_after_nodisc.getvalue())
            self.assertIn("PR #99", buf_after_nodisc.getvalue())

            # With discovery excluding Claude: not due in probe or followups --due
            probe_other = _Args(**{**probe_after.__dict__, "available": "antigravity:gemini-4-neon"})
            buf_probe_other = io.StringIO()
            with mock.patch("orch._infer_probe", return_value=None), contextlib.redirect_stdout(buf_probe_other):
                orch.probe(probe_other)
            self.assertNotIn("followup due", buf_probe_other.getvalue())

            fu_other = _Args(**{**fu_after_nodisc.__dict__, "available": "antigravity:gemini-4-neon"})
            buf_fu_other = io.StringIO()
            with contextlib.redirect_stdout(buf_fu_other):
                orch.followups_cmd(fu_other)
            self.assertNotIn("followup due", buf_fu_other.getvalue())

            # With discovery including Claude: both probe and followups --due report it due
            probe_with_claude = _Args(
                **{**probe_after.__dict__, "available": "claude:claude-sonnet-5-5,antigravity:gemini-4-neon"}
            )
            buf_probe_claude = io.StringIO()
            with mock.patch("orch._infer_probe", return_value=None), contextlib.redirect_stdout(buf_probe_claude):
                orch.probe(probe_with_claude)
            self.assertIn("followup due", buf_probe_claude.getvalue())
            self.assertIn("PR #99", buf_probe_claude.getvalue())
            self.assertIn("claude", buf_probe_claude.getvalue())

            fu_after = _Args(
                **{**fu_after_nodisc.__dict__, "available": "claude:claude-sonnet-5-5,antigravity:gemini-4-neon"}
            )
            buf_after = io.StringIO()
            with contextlib.redirect_stdout(buf_after):
                orch.followups_cmd(fu_after)
            self.assertIn("followup due", buf_after.getvalue())
            self.assertIn("PR #99", buf_after.getvalue())

            # Mark done -> removed from assets.yaml
            fu_done = _Args(**{**fu_after.__dict__, "done": "PR #99", "due": False})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.followups_cmd(fu_done), 0)
            self.assertFalse(orch.load_file(assets_path).get("followups"))

    def test_not_offered_repro_proceeds_with_note_no_reset_no_followup_no_due(self):
        example_assets = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "assets", "assets.example.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "a.yaml")
            with open(example_assets, encoding="utf-8") as src, open(assets_path, "w", encoding="utf-8") as dst:
                dst.write(src.read())

            for mode in (None, "proceed", "defer", "ask"):
                sel_args = _Args(
                    assets=assets_path,
                    catalog=CATALOG_PATH,
                    now="2026-11-20T21:00:00Z",
                    complexity="standard",
                    risk="medium",
                    available="codex:gpt-6-2-sol,antigravity:gemini-4-neon",
                    ui=True,
                    fail_on_low=False,
                    prefer_family=None,
                    fallback=mode,
                    ref="demo",
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = orch.select(sel_args)
                self.assertEqual(rc, 0, msg=f"mode={mode} should never block when preferred family is not offered here")
                out = buf.getvalue()
                self.assertIn("[note]", out)
                self.assertIn("not offered", out)
                self.assertNotIn("[provisional]", out)
                self.assertNotIn("DEFER", out)
                self.assertNotIn("ASK", out)
                self.assertNotIn("2026-12-02", out)
                self.assertNotIn("earliest reset", out)
                self.assertNotIn("followup recorded", out)
                self.assertNotIn("followup due", out)
                self.assertIn("implementer: codex / gpt-6-2-sol @ low", out)
                self.assertFalse(orch.load_file(assets_path).get("followups"))

    def test_not_offered_without_assets_account_or_with_prefer_family_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "codex-only.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  ui_fallback: defer\n"
                    "providers:\n"
                    "  - id: codex\n"
                    "    policy: normal\n"
                    "    status: ok\n"
                    "  - id: anthropic-api\n"
                    "    policy: avoid\n"
                    "    pay_per_use: true\n"
                    "    status: ok\n"
                )

            for mode in (None, "proceed", "defer", "ask"):
                sel_args = _Args(
                    assets=assets_path,
                    catalog=CATALOG_PATH,
                    now="2026-11-20T21:00:00Z",
                    complexity="standard",
                    risk="medium",
                    available="codex:gpt-6-2-sol",
                    ui=True,
                    fail_on_low=False,
                    prefer_family=None,
                    fallback=mode,
                    ref="codex-ui",
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = orch.select(sel_args)
                self.assertEqual(rc, 0, msg=f"mode={mode} should never block")
                out = buf.getvalue()
                self.assertIn("[note]", out)
                self.assertIn("not offered", out)
                self.assertNotIn("[provisional]", out)
                self.assertNotIn("DEFER", out)
                self.assertNotIn("ASK", out)
                self.assertNotIn("followup recorded", out)
                self.assertIn("implementer: codex / gpt-6-2-sol @ low", out)
                self.assertFalse(orch.load_file(assets_path).get("followups"))

            # Repo --prefer-family absent from this harness also proceeds with a note and no follow-up
            sel_pf = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="codex:gpt-6-2-sol",
                ui=False,
                fail_on_low=False,
                prefer_family="anthropic",
                fallback="defer",
                ref="codex-pf",
            )
            buf_pf = io.StringIO()
            with contextlib.redirect_stdout(buf_pf):
                rc_pf = orch.select(sel_pf)
            self.assertEqual(rc_pf, 0)
            self.assertIn("[note]", buf_pf.getvalue())
            self.assertIn("anthropic", buf_pf.getvalue())
            self.assertNotIn("[provisional]", buf_pf.getvalue())
            self.assertFalse(orch.load_file(assets_path).get("followups"))

            # When a local model carries matching strengths (e.g. ui-ux-review), it satisfies --ui directly
            local_cat = os.path.join(tmp, "catalog.local.yaml")
            with open(local_cat, "w", encoding="utf-8") as fh:
                fh.write(
                    "models:\n"
                    "  - id: gpt-6-1-terra\n"
                    "    name: GPT-6.1 Terra\n"
                    "    family: openai\n"
                    "    cost: normal\n"
                    "    rating: rated\n"
                    "    scores: {high: 42.43}\n"
                    "    source: aa-v5.0.1\n"
                    "    strengths: [general-coding, ui-ux-review]\n"
                )
            sel_strength = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="codex:gpt-6-2-sol,codex:gpt-6-1-terra",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback="defer",
                ref="codex-strength",
            )
            buf_str = io.StringIO()
            with contextlib.redirect_stdout(buf_str):
                rc_str = orch.select(sel_strength)
            self.assertEqual(rc_str, 0)
            self.assertIn("implementer: codex / gpt-6-1-terra @ high", buf_str.getvalue())
            self.assertNotIn("not offered", buf_str.getvalue())
            self.assertNotIn("[provisional]", buf_str.getvalue())

    def test_exhausted_reset_time_ignores_unoffered_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_anthropic_exhausted_assets(assets_path, ui_fallback="proceed")
            # Offer ONLY antigravity:claude-sonnet-5-5 (resets 2026-11-27), NOT claude (resets 2026-11-23).
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="standard",
                risk="low",
                available="antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon",
                ui=True,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="PR #101",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("2026-11-27T15:59:36+00:00", out)
            self.assertNotIn("2026-11-23T08:00:00+00:00", out)
            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(fus[0]["after"], "2026-11-27T15:59:36+00:00")

            # Recorded follow-up is NOT due in a harness that does not offer Claude even after reset
            sel_other = _Args(
                **{
                    **sel_args.__dict__,
                    "now": "2026-11-28T00:00:00Z",
                    "available": "antigravity:gemini-4-neon",
                    "ui": False,
                }
            )
            buf_other = io.StringIO()
            with contextlib.redirect_stdout(buf_other):
                self.assertEqual(orch.select(sel_other), 0)
            self.assertNotIn("followup due", buf_other.getvalue())

            # It IS due once discovery includes a usable Claude model after reset
            sel_due = _Args(
                **{
                    **sel_args.__dict__,
                    "now": "2026-11-28T00:00:00Z",
                    "available": "antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon",
                    "ui": False,
                }
            )
            buf_due = io.StringIO()
            with contextlib.redirect_stdout(buf_due):
                self.assertEqual(orch.select(sel_due), 0)
            self.assertIn("followup due", buf_due.getvalue())
            self.assertIn("PR #101", buf_due.getvalue())


class TestDecisionLedger(unittest.TestCase):
    def test_log_appends_jsonl_next_to_assets_and_strips_repo_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            ledger_path = os.path.join(tmp, "ledger.jsonl")

            sel_log = _Args(
                assets=assets_path,
                ledger=None,
                now="2026-11-20T21:00:00Z",
                event="select",
                complexity="hard",
                risk="high",
                ui=True,
                provider="antigravity",
                model="gemini-4-neon",
                effort="high",
                reviewer="claude/claude-opus-5-5@xhigh",
                outcome=None,
                findings=None,
                repo="/home/someone/private/demo-app/",
                provisional=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.log_cmd(sel_log), 0)

            done_log = _Args(
                assets=assets_path,
                ledger=None,
                now="2026-11-20T21:30:00Z",
                event=None,
                complexity="hard",
                risk="high",
                ui=True,
                provider="antigravity",
                model="gemini-4-neon",
                effort="high",
                reviewer="claude/claude-opus-5-5@xhigh",
                outcome="ok",
                findings=2,
                repo="/home/someone/private/demo-app",
                provisional=False,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.log_cmd(done_log), 0)

            self.assertTrue(os.path.isfile(ledger_path))
            with open(ledger_path, encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("/home/someone/private", raw)
            rows = [json.loads(line) for line in raw.strip().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["event"], "select")
            self.assertEqual(rows[0]["repo"], "demo-app")
            self.assertEqual(rows[0]["surface"], "subagent")
            self.assertEqual(rows[0]["ui"], True)
            self.assertEqual(rows[0]["provisional"], True)
            self.assertIsNone(rows[0]["outcome"])
            self.assertEqual(rows[1]["event"], "complete")
            self.assertEqual(rows[1]["outcome"], "ok")
            self.assertEqual(rows[1]["findings"], 2)
            self.assertEqual(rows[1]["reviewer"], "claude/claude-opus-5-5@xhigh")

    def test_log_follows_env_assets_override_and_records_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "custom-assets.yaml")
            expected_ledger = os.path.join(tmp, "ledger.jsonl")
            args = _Args(
                assets=None,
                ledger=None,
                now="2026-11-20T21:00:00Z",
                event=None,
                complexity="standard",
                risk="low",
                ui=False,
                surface="conversation",
                provider="codex",
                model="gpt-6-2-sol",
                effort="low",
                reviewer=None,
                outcome="rework",
                findings=1,
                repo="sample-repo",
                provisional=False,
            )
            with mock.patch.dict(os.environ, {"AGENT_ORCHESTRATION_ASSETS": assets_path}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(orch.log_cmd(args), 0)
            self.assertTrue(os.path.isfile(expected_ledger))
            with open(expected_ledger, encoding="utf-8") as fh:
                row = json.loads(fh.read().strip())
            self.assertEqual(row["outcome"], "rework")
            self.assertEqual(row["surface"], "conversation")


class TestCheckExtensions(unittest.TestCase):
    def test_check_validates_ui_fallback_new_conversations_and_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  ui_fallback: maybe\n"
                    "  new_conversations: always\n"
                    "providers: []\n"
                )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.check(_Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 1)
            self.assertIn("ui_fallback", buf.getvalue())
            self.assertIn("new_conversations", buf.getvalue())

            good = os.path.join(tmp, "good.yaml")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  ui_fallback: ask\n"
                    "  new_conversations: allow\n"
                    "providers: []\n"
                    "followups:\n"
                    "  - kind: ui-review\n"
                    '    ref: "PR #1"\n'
                    "    needs: ui-ux-review\n"
                    "    after: 2026-11-23T08:00:00+00:00\n"
                )
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = orch.check(_Args(assets=good, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc2, 0)

    def test_check_suggests_update_when_lockfile_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write("version: 1\nproviders: []\n")
            lock_path = os.path.join(tmp, ".skill-lock.json")
            with open(lock_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": 3,
                        "skills": {
                            "agent-orchestration": {
                                "source": "osnoser1/agent-orchestration",
                                "updatedAt": "2026-11-01T12:00:00Z",
                            }
                        },
                    },
                    fh,
                )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.check(
                    _Args(assets=assets_path, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=lock_path)
                )
            self.assertEqual(rc, 0)
            self.assertIn("npx skills update agent-orchestration", buf.getvalue())

            # Fresh lockfile -> no update suggestion
            with open(lock_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": 3,
                        "skills": {
                            "agent-orchestration": {
                                "source": "osnoser1/agent-orchestration",
                                "updatedAt": "2026-11-19T12:00:00Z",
                            }
                        },
                    },
                    fh,
                )
            buf_fresh = io.StringIO()
            with contextlib.redirect_stdout(buf_fresh):
                rc_fresh = orch.check(
                    _Args(assets=assets_path, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=lock_path)
                )
            self.assertEqual(rc_fresh, 0)
            self.assertNotIn("npx skills update", buf_fresh.getvalue())


class TestCheckAvailable(unittest.TestCase):
    ASSETS = (
        "version: 1\n"
        "providers:\n"
        "  - id: claude\n"
        "  - id: codex\n"
        "  - id: antigravity\n"
        "followups:\n"
        "  - kind: resume\n"
        '    ref: "batch-a"\n'
        "    needs: provider:codex/hard\n"
        "  - kind: resume\n"
        '    ref: "batch-b"\n'
        "    needs: provider:claude/claude-sonnet-5-5\n"
        "  - kind: resume\n"
        '    ref: "batch-c"\n'
        "    needs: hard\n"
    )
    LOCAL = (
        "models:\n"
        "  - id: local-model-x\n"
        "    name: Local X\n"
        "    family: local\n"
        "    cost: cheap\n"
        "    rating: unrated\n"
    )

    def _check(self, tmp, **kw):
        assets = os.path.join(tmp, "assets.yaml")
        _write(assets, self.ASSETS)
        _write(os.path.join(tmp, "catalog.local.yaml"), self.LOCAL)
        args = _Args(assets=assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="", **kw)
        return _run(orch.check, args)

    def test_notes_unavailable_providers_local_models_and_followup_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._check(tmp, available="claude:claude-opus-5-5")
        self.assertEqual(rc, 0)
        self.assertIn("ok: catalog and assets valid", out)
        self.assertIn("[note] assets provider codex has no available model", out)
        self.assertIn("[note] assets provider antigravity has no available model", out)
        self.assertNotIn("assets provider claude has no", out)
        self.assertIn("[note] catalog.local.yaml model local-model-x is not in the available list", out)
        self.assertIn("[note] followup batch-a needs provider:codex/hard", out)
        self.assertIn("[note] followup batch-b needs provider:claude/claude-sonnet-5-5", out)
        self.assertNotIn("followup batch-c", out)

    def test_no_notes_when_everything_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._check(
                tmp,
                available="claude:claude-sonnet-5-5, codex:gpt-6-2-sol,antigravity:gemini-4-neon,x:local-model-x",
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("[note] assets provider", out)
        self.assertNotIn("catalog.local.yaml model", out)
        self.assertNotIn("[note] followup", out)

    def test_three_part_group_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, "assets.yaml")
            _write(
                assets,
                "version: 1\nproviders:\n  - id: antigravity\n  - id: claude\nfollowups:\n"
                '  - {kind: resume, ref: "g-missing", needs: "provider:antigravity/Gemini Models/hard"}\n'
                '  - {kind: resume, ref: "g-present", needs: "provider:claude/Pool/hard"}\n'
                '  - {kind: resume, ref: "g-model", needs: "provider:claude/Pool/claude-opus-5-5"}\n',
            )
            args = _Args(
                assets=assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="",
                available="claude:claude-sonnet-5-5",
            )
            rc, out = _run(orch.check, args)
        self.assertEqual(rc, 0)
        self.assertIn("[note] followup g-missing needs provider:antigravity/Gemini Models/hard", out)
        self.assertNotIn("g-present", out)
        self.assertIn("[note] followup g-model needs provider:claude/Pool/claude-opus-5-5", out)

    def test_aliases_and_display_names_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, "assets.yaml")
            _write(
                assets,
                "version: 1\nproviders:\n  - id: antigravity\nfollowups:\n"
                '  - {kind: resume, ref: "by-id", needs: "provider:antigravity/gemini-pro-orchestrator-neon"}\n'
                '  - {kind: resume, ref: "bare", needs: "gemini-pro-orchestrator-neon"}\n',
            )
            _write(
                os.path.join(tmp, "catalog.local.yaml"),
                "models:\n  - id: gemini-pro-orchestrator-neon\n    name: Orchestrator (Neon)\n"
                "    aliases: [gemini-4-orchestrator-neon]\n    family: google\n    cost: normal\n    rating: unrated\n",
            )
            args = _Args(
                assets=assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="",
                available="antigravity:gemini-4-orchestrator-neon",
            )
            rc, out = _run(orch.check, args)
        self.assertEqual(rc, 0)
        self.assertNotIn("[note]", out)

    def test_malformed_pair_is_noted_and_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._check(tmp, available="claude-opus-5-5,claude:claude-opus-5-5,:x")
        self.assertEqual(rc, 0)
        self.assertIn("[note] --available entry claude-opus-5-5 is not provider:model; ignored", out)
        self.assertIn("[note] --available entry :x is not provider:model; ignored", out)
        self.assertNotIn("assets provider claude has no", out)

    def test_missing_assets_file_still_notes_malformed_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _Args(
                assets=os.path.join(tmp, "absent.yaml"), catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z",
                lockfile="", available="oops",
            )
            rc, out = _run(orch.check, args)
        self.assertEqual(rc, 0)
        self.assertIn("no assets file", out)
        self.assertIn("[note] --available entry oops is not provider:model; ignored", out)

    def test_without_flag_behaviour_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._check(tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ok: catalog and assets valid")


class TestProbeMergeAndExhaustedThreshold(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)

    def test_exhaust_group_and_model_preserved_across_probe_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )

            fake_proc = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr=""
            )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                low_threshold=None,
                exhausted_threshold=None,
                available=None,
            )
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.probe(probe_args), 0)

            # Exhaust Gemini Models via --model gemini-4-neon with relative duration "Resets in 3h13m7s."
            exhaust_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                message="Individual quota reached… Resets in 3h13m7s.",
                group=None,
                model="gemini-4-neon",
            )
            buf_ex = io.StringIO()
            with contextlib.redirect_stdout(buf_ex):
                self.assertEqual(orch.exhaust(exhaust_args), 0)
            self.assertIn("antigravity (Gemini Models): exhausted until 2026-11-21T00:13:07+00:00", buf_ex.getvalue())

            loaded_after_ex = orch.load_file(assets_path)
            ag_ex = loaded_after_ex["providers"][0]
            self.assertEqual(ag_ex["status"], "ok")  # Gemini 4 Argon is still ok
            by_name_ex = {g["name"]: g for g in ag_ex["groups"]}
            self.assertEqual(by_name_ex["Gemini Models"]["status"], "exhausted")
            self.assertEqual(by_name_ex["Gemini Models"]["exhausted_until"], "2026-11-21T00:13:07+00:00")
            self.assertIn("Resets in 3h13m7s", by_name_ex["Gemini Models"]["status_note"])

            # Next probe (5 minutes later, same usage fixture) MUST merge and preserve Gemini Models exhaustion!
            probe_later = _Args(**{**probe_args.__dict__, "now": "2026-11-20T21:05:00Z"})
            buf_probe = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf_probe):
                self.assertEqual(orch.probe(probe_later), 0)
            out_probe = buf_probe.getvalue()
            self.assertIn("antigravity: ok (1/3 groups usable)", out_probe)
            self.assertIn("group Gemini Models: exhausted", out_probe)

            loaded_after_probe = orch.load_file(assets_path)
            by_name_probe = {g["name"]: g for g in loaded_after_probe["providers"][0]["groups"]}
            self.assertEqual(by_name_probe["Gemini Models"]["status"], "exhausted")
            self.assertEqual(by_name_probe["Gemini Models"]["exhausted_until"], "2026-11-21T00:13:07+00:00")
            self.assertEqual(by_name_probe["Gemini 4 Argon"]["status"], "ok")

            # Selection skips gemini-4-neon (Gemini Models exhausted) and picks gemini-4-argon
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:05:00Z",
                complexity="hard",
                risk="low",
                available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref=None,
            )
            buf_sel = io.StringIO()
            with contextlib.redirect_stdout(buf_sel):
                self.assertEqual(orch.select(sel_args), 0)
            self.assertIn("implementer: antigravity / gemini-4-argon @ high", buf_sel.getvalue())
            self.assertIn("antigravity:gemini-4-neon: group Gemini Models exhausted", buf_sel.getvalue())

            # If a probe before exhausted_until shows Gemini Models genuinely recovered (0% used), clear exhaustion
            recovered_data = copy.deepcopy(self.fixture)
            for b in recovered_data["command"]["data"]["groups"][0]["buckets"]:
                b["remaining_fraction"] = 1.0
            fake_recovered = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(recovered_data), stderr=""
            )
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_recovered
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.probe(probe_later), 0)
            by_name_rec = {g["name"]: g for g in orch.load_file(assets_path)["providers"][0]["groups"]}
            self.assertEqual(by_name_rec["Gemini Models"]["status"], "ok")
            self.assertNotIn("exhausted_until", by_name_rec["Gemini Models"])

    def test_provider_level_exhaust_preserved_by_probe_until_expired_or_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )

            # Provider-level exhaust without --group or --model
            exhaust_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                message="Individual quota reached… Resets in 3h13m7s.",
                group=None,
                model=None,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.exhaust(exhaust_args), 0)

            fake_proc = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr=""
            )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:05:00Z",
                provider="antigravity",
                low_threshold=None,
                exhausted_threshold=None,
                available=None,
            )
            buf = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf):
                rc = orch.probe(probe_args)
            self.assertEqual(rc, 4)
            ag = orch.load_file(assets_path)["providers"][0]
            self.assertEqual(ag["status"], "exhausted")
            self.assertEqual(ag["exhausted_until"], "2026-11-21T00:13:07+00:00")

            # After exhausted_until expires (2026-11-21T00:15:00Z), probe clears lockout to ok (2/3 groups usable)
            probe_expired = _Args(**{**probe_args.__dict__, "now": "2026-11-21T00:15:00Z"})
            buf_exp = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf_exp):
                rc_exp = orch.probe(probe_expired)
            self.assertEqual(rc_exp, 0)
            self.assertIn("antigravity: ok (2/3 groups usable)", buf_exp.getvalue())
            ag_exp = orch.load_file(assets_path)["providers"][0]
            self.assertEqual(ag_exp["status"], "ok")
            self.assertNotIn("exhausted_until", ag_exp)

    def test_near_exhaustion_99_3_percent_marks_exhausted_by_default_and_configurable(self):
        near_data = copy.deepcopy(self.fixture)
        # Set Gemini Models 5h bucket to 99.3% used (remaining_fraction = 0.007)
        near_data["command"]["data"]["groups"][0]["buckets"][1]["remaining_fraction"] = 0.007

        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )

            fake_proc = subprocess.CompletedProcess(
                args=["agy"], returncode=0, stdout=json.dumps(near_data), stderr=""
            )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="antigravity",
                low_threshold=None,
                exhausted_threshold=None,
                available=None,
            )
            buf = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf):
                rc = orch.probe(probe_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("antigravity: ok (1/3 groups usable)", out)
            self.assertIn("group Gemini Models: exhausted (99.3% of 5h", out)

            loaded = orch.load_file(assets_path)
            by_name = {g["name"]: g for g in loaded["providers"][0]["groups"]}
            self.assertEqual(by_name["Gemini Models"]["status"], "exhausted")
            self.assertEqual(by_name["Gemini Models"]["used_percent"], 99.3)
            self.assertEqual(by_name["Gemini Models"]["window"], "5h")

            # Raising preferences.exhausted_threshold to 99.5 marks 99.3% as low instead of exhausted
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  low_threshold: 85\n"
                    "  exhausted_threshold: 99.5\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    probe: {kind: antigravity}\n"
                )
            buf_custom = io.StringIO()
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch(
                "subprocess.run", return_value=fake_proc
            ), contextlib.redirect_stdout(buf_custom):
                self.assertEqual(orch.probe(probe_args), 0)
            by_name_custom = {g["name"]: g for g in orch.load_file(assets_path)["providers"][0]["groups"]}
            self.assertEqual(by_name_custom["Gemini Models"]["status"], "low")

    def test_scheduled_resume_when_no_candidate_left_deduplicates_and_excludes_emergency_and_pay_per_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: claude\n"
                    "    policy: preserve\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-21T00:13:07+00:00\n"
                    "    groups:\n"
                    '      - name: "Gemini Models"\n'
                    "        models: [gemini-4-neon]\n"
                    "        status: exhausted\n"
                    "        used_percent: 99.3\n"
                    "        window: 5h\n"
                    "        resets_at: 2026-11-21T00:13:07+00:00\n"
                    "  - id: openai-api\n"
                    "    policy: avoid\n"
                    "    pay_per_use: true\n"
                    "    status: ok\n"
                )

            # Even with emergency (claude-fable-5-2) and pay-per-use (openai-api:gpt-6-2-sol) in available,
            # select exits 4, computes earliest reset from non-emergency/non-pay-per-use tier candidates
            # (2026-11-21T00:13:07+00:00 + 2m = 2026-11-21T00:15:07+00:00), and records kind: resume once.
            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="low",
                available="claude:claude-opus-5-5,claude:claude-fable-5-2,antigravity:gemini-4-neon,openai-api:gpt-6-2-sol",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                ref="thread-child-1",
            )
            buf1 = io.StringIO()
            with contextlib.redirect_stdout(buf1):
                rc1 = orch.select(sel_args)
            self.assertEqual(rc1, 4)
            out1 = buf1.getvalue()
            self.assertIn("SCHEDULE_RESUME", out1)
            self.assertIn("2026-11-21T00:13:07+00:00", out1)
            self.assertIn("2026-11-21T00:15:07+00:00", out1)
            self.assertIn("followup recorded: [resume] thread-child-1 (needs T3, after 2026-11-21T00:15:07+00:00)", out1)

            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(
                fus[0],
                {
                    "kind": "resume",
                    "ref": "thread-child-1",
                    "needs": "T3",
                    "after": "2026-11-21T00:15:07+00:00",
                    "on_recovery": True,
                },
            )

            # Second conversation / retry with same ref does not schedule a duplicate
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = orch.select(sel_args)
            self.assertEqual(rc2, 4)
            self.assertIn("followup already scheduled: [resume] thread-child-1", buf2.getvalue())
            self.assertEqual(len(orch.load_file(assets_path).get("followups") or []), 1)

            # Before reset: followups --due does not mark it due (even if pay-per-use or emergency is in available)
            fu_before = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T22:00:00Z",
                available="claude:claude-fable-5-2,openai-api:gpt-6-2-sol,antigravity:gemini-4-neon",
                due=True,
                add=False,
                done=None,
            )
            buf_before = io.StringIO()
            with contextlib.redirect_stdout(buf_before):
                self.assertEqual(orch.followups_cmd(fu_before), 0)
            _assert_none_due(self, buf_before.getvalue())

            # After antigravity resets (2026-11-21T00:16:00Z): due when antigravity:gemini-4-neon is in available
            fu_after = _Args(
                **{
                    **fu_before.__dict__,
                    "now": "2026-11-21T00:16:00Z",
                    "available": "antigravity:gemini-4-neon",
                }
            )
            buf_after = io.StringIO()
            with contextlib.redirect_stdout(buf_after):
                self.assertEqual(orch.followups_cmd(fu_after), 0)
            self.assertIn("followup due: [resume] thread-child-1 (needs T3; usable via antigravity (Gemini Models))", buf_after.getvalue())

            # Clear when the child resumes
            fu_done = _Args(**{**fu_after.__dict__, "done": "thread-child-1", "due": False})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.followups_cmd(fu_done), 0)
            self.assertFalse(orch.load_file(assets_path).get("followups"))

    def test_expired_group_lockout_clears_once_without_duplicate_provider_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-21T00:13:07+00:00\n"
                    "    groups:\n"
                    '      - name: "Gemini Models"\n'
                    "        models: [gemini-4-neon]\n"
                    "        status: exhausted\n"
                    "        used_percent: 99.3\n"
                    "        window: 5h\n"
                    "        resets_at: 2026-11-21T00:13:07+00:00\n"
                )
            probe_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-21T00:20:00Z",
                provider=None,
                low_threshold=None,
                exhausted_threshold=None,
                available=None,
            )
            buf1 = io.StringIO()
            with mock.patch("orch._infer_probe", return_value=None), contextlib.redirect_stdout(buf1):
                self.assertEqual(orch.probe(probe_args), 0)
            lines1 = [l for l in buf1.getvalue().splitlines() if "expired lockout cleared" in l]
            self.assertEqual(lines1, ["antigravity (Gemini Models): expired lockout cleared -> ok"])

            # Running probe a second time produces no duplicate recovery line
            buf2 = io.StringIO()
            with mock.patch("orch._infer_probe", return_value=None), contextlib.redirect_stdout(buf2):
                self.assertEqual(orch.probe(probe_args), 0)
            self.assertNotIn("expired lockout cleared", buf2.getvalue())


class TestReviewerFallbackOrder(unittest.TestCase):
    """select always names a reviewer when any non-excluded model exists (references/selection.md)."""

    def _select(self, available, complexity="standard", risk="low", assets_text=None, implementer=None):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            if assets_text is not None:
                with open(assets_path, "w", encoding="utf-8") as fh:
                    fh.write(assets_text)
            args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity=complexity,
                risk=risk,
                available=available,
                ui=False,
                fail_on_low=False,
                implementer=implementer,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(args)
            return rc, buf.getvalue()

    def test_stage1_different_family_meeting_tier_has_no_degradation_note(self):
        rc, out = self._select("codex:gpt-6-2-sol,claude:claude-sonnet-5-5")
        self.assertEqual(rc, 0)
        self.assertIn("reviewer (T2, different family): claude / claude-sonnet-5-5", out)
        self.assertNotIn("reviewer degraded", out)

    def test_stage3_different_family_below_tier_picks_best_score_and_notes(self):
        rc, out = self._select(
            "codex:gpt-6-2-sol,claude:claude-haiku-5-5,claude:claude-sonnet-5-5",
            risk="high",
        )
        self.assertEqual(rc, 0)
        self.assertIn("implementer: codex / gpt-6-2-sol", out)
        self.assertIn("reviewer (T4, different family, below tier): claude / claude-sonnet-5-5 @ xhigh", out)
        self.assertIn("[note] reviewer degraded: no different-family or same-family candidate meets T4", out)

    def test_stage2_same_family_meeting_tier(self):
        rc, out = self._select("claude:claude-opus-5-5,claude:claude-sonnet-5-5")
        self.assertEqual(rc, 0)
        self.assertIn("implementer: claude / claude-sonnet-5-5", out)
        self.assertIn("reviewer (T2, same family, different model, fresh context): claude / claude-opus-5-5", out)
        self.assertIn("[note] reviewer degraded: no different-family candidate meets T2", out)
        self.assertNotIn("below tier", out)

    def test_same_family_meeting_tier_beats_different_family_below_tier(self):
        rc, out = self._select("claude:claude-opus-5-5,claude:claude-sonnet-5-5,codex:gpt-6-luna")
        self.assertEqual(rc, 0)
        self.assertIn("implementer: claude / claude-sonnet-5-5", out)
        self.assertIn("reviewer (T2, same family, different model, fresh context): claude / claude-opus-5-5", out)
        self.assertNotIn("gpt-6-luna @", out.split("reviewer (", 1)[1].splitlines()[0])

    def test_different_family_below_tier_beats_same_family_below_tier(self):
        rc, out = self._select(
            "claude:claude-opus-5-5,claude:claude-sonnet-5-5,codex:gpt-6-1-terra", complexity="hard"
        )
        self.assertEqual(rc, 0)
        self.assertIn("implementer: claude / claude-opus-5-5 @ high", out)
        self.assertIn("reviewer (T3, different family, below tier): codex / gpt-6-1-terra @ xhigh", out)

    def test_stage4_same_family_below_tier_when_nothing_meets(self):
        rc, out = self._select("claude:claude-opus-5-5,claude:claude-sonnet-5-5", complexity="hard")
        self.assertEqual(rc, 0)
        self.assertIn("implementer: claude / claude-opus-5-5 @ high", out)
        self.assertIn(
            "reviewer (T3, same family, different model, fresh context, below tier): claude / claude-sonnet-5-5 @ xhigh",
            out,
        )
        self.assertIn("[note] reviewer degraded", out)

    def test_stage5_same_model_fresh_conversation_when_single_model(self):
        rc, out = self._select("codex:gpt-6-2-sol")
        self.assertEqual(rc, 0)
        self.assertIn("reviewer (T2, same model, fresh conversation): codex / gpt-6-2-sol", out)
        self.assertIn("[note] reviewer degraded: no other model usable", out)

    def test_emergency_pay_per_use_and_exhausted_stay_excluded(self):
        assets = (
            "version: 1\n"
            "providers:\n"
            "  - id: claude\n"
            "    status: exhausted\n"
            "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: api\n"
            "    pay_per_use: true\n"
            "  - id: codex\n"
            "    status: ok\n"
        )
        rc, out = self._select(
            "codex:gpt-6-2-sol,claude:claude-sonnet-5-5,api:gemini-4-neon,codex:gpt-6-2-astra",
            assets_text=assets,
        )
        self.assertEqual(rc, 0)
        self.assertIn("reviewer (T2, same model, fresh conversation): codex / gpt-6-2-sol", out)
        self.assertNotIn("reviewer (T2, different family", out)

    def test_implementer_flag_ranks_reviewer_against_the_model_that_ran(self):
        rc, out = self._select(
            "claude:claude-opus-5-5,codex:gpt-6-2-sol",
            complexity="hard",
            implementer="claude-opus-5-5",
        )
        self.assertEqual(rc, 0)
        self.assertIn("implementer (given): anthropic / claude-opus-5-5", out)
        self.assertIn("reviewer (T3, different family): codex / gpt-6-2-sol", out)

    def test_implementer_flag_falls_back_when_other_family_exhausted(self):
        assets = (
            "version: 1\n"
            "providers:\n"
            "  - id: codex\n"
            "    status: exhausted\n"
            "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: claude\n"
            "    status: ok\n"
        )
        rc, out = self._select(
            "claude:claude-opus-5-5,claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
            assets_text=assets,
            implementer="claude-opus-5-5",
        )
        self.assertEqual(rc, 0)
        self.assertIn("reviewer (T2, same family, different model, fresh context): claude / claude-sonnet-5-5", out)

    def test_implementer_flag_reports_no_reviewer_when_everything_is_exhausted(self):
        assets = (
            "version: 1\n"
            "providers:\n"
            "  - id: claude\n"
            "    status: exhausted\n"
            "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
        )
        rc, out = self._select("claude:claude-opus-5-5", assets_text=assets, implementer="claude-opus-5-5")
        self.assertEqual(rc, 4)
        self.assertIn("NO REVIEWER", out)

    def test_implementer_flag_rejects_unknown_model(self):
        rc, out = self._select("codex:gpt-6-2-sol", implementer="not-a-model")
        self.assertEqual(rc, 1)
        self.assertIn("not in catalog", out)


class TestSupervisionAndCheckpointsExtensions(unittest.TestCase):
    def setUp(self):
        self.catalog = orch.load_file(CATALOG_PATH)

    def test_catalog_includes_antigravity_orchestrator_neon_and_argon_unrated(self):
        by_id = {m["id"]: m for m in self.catalog.get("models") or []}
        self.assertIn("gemini-pro-orchestrator-neon", by_id)
        self.assertIn("gemini-pro-orchestrator-g1-pro", by_id)

        neon = by_id["gemini-pro-orchestrator-neon"]
        self.assertEqual(neon["name"], "Orchestrator (Neon)")
        self.assertEqual(neon["family"], "google")
        self.assertEqual(neon["rating"], "unrated")
        self.assertIn("subagents", neon.get("notes", ""))
        self.assertIn("Orchestrator (Neon)", neon.get("aliases") or [])

        argon = by_id["gemini-pro-orchestrator-g1-pro"]
        self.assertEqual(argon["name"], "Orchestrator (Argon)")
        self.assertEqual(argon["family"], "google")
        self.assertEqual(argon["rating"], "unrated")
        self.assertIn("subagents", argon.get("notes", ""))
        self.assertIn("Orchestrator (Argon)", argon.get("aliases") or [])

        name_map = orch._catalog_name_map(self.catalog)
        self.assertEqual(name_map.get(orch.norm("Orchestrator (Neon)")), "gemini-pro-orchestrator-neon")
        self.assertEqual(name_map.get(orch.norm("Orchestrator (Argon)")), "gemini-pro-orchestrator-g1-pro")
        self.assertEqual(name_map.get(orch.norm("gemini-pro-orchestrator-argon")), "gemini-pro-orchestrator-g1-pro")

    def test_check_validates_catalog_aliases_and_rejects_bare_provider_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_cat = os.path.join(tmp, "bad_catalog.yaml")
            with open(bad_cat, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "models:\n"
                    "  - id: test-model\n"
                    "    name: Test Model\n"
                    "    family: google\n"
                    "    cost: normal\n"
                    "    rating: unrated\n"
                    "    aliases: not-a-list\n"
                )
            bad_assets = os.path.join(tmp, "bad_assets.yaml")
            with open(bad_assets, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers: []\n"
                    "followups:\n"
                    "  - kind: resume\n"
                    '    ref: "b1"\n'
                    "    needs: provider:claude-extra\n"
                )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.check(_Args(assets=bad_assets, catalog=bad_cat, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 1)
            self.assertIn("aliases", buf.getvalue())
            self.assertIn("provider:claude-extra", buf.getvalue())

    def test_check_validates_design_checkpoints_and_evidence_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  design_checkpoints: auto\n"
                    "  evidence: true\n"
                    "providers: []\n"
                )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.check(_Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 1)
            self.assertIn("design_checkpoints", buf.getvalue())
            self.assertIn("evidence", buf.getvalue())

            good = os.path.join(tmp, "good.yaml")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "preferences:\n"
                    "  design_checkpoints: delegate\n"
                    '  evidence: "red-green tests; skip recordings unless UI task"\n'
                    "providers: []\n"
                )
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = orch.check(_Args(assets=good, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc2, 0)

    def test_deferred_launch_until_specific_provider_and_group_renews_enforces_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "version: 1\n"
                    "providers:\n"
                    "  - id: claude\n"
                    "    policy: preserve\n"
                    "    status: ok\n"
                    "  - id: claude-extra\n"
                    "    policy: normal\n"
                    "    status: exhausted\n"
                    "    exhausted_until: 2026-11-25T00:00:00+00:00\n"
                    "  - id: antigravity\n"
                    "    policy: spend-first\n"
                    "    status: ok\n"
                    "    groups:\n"
                    '      - name: "Gemini Models"\n'
                    "        models: [gemini-4-neon, gemini-4-hydrogen]\n"
                    "        status: exhausted\n"
                    "        used_percent: 100\n"
                    "        window: 7d\n"
                    "        resets_at: 2026-11-26T00:00:00+00:00\n"
                    '      - name: "Claude and GPT models"\n'
                    "        models: [claude-opus-5-5, claude-sonnet-5-5]\n"
                    "        status: ok\n"
                    "        used_percent: 20\n"
                    "        window: 7d\n"
                    "        resets_at: 2026-11-27T00:00:00+00:00\n"
                )

            # Bare provider:<id> is not accepted as a valid match
            self.assertEqual(
                orch.find_usable_in_available(
                    self.catalog,
                    orch.load_file(assets_path),
                    orch.get_now("2026-11-25T00:05:00Z"),
                    "provider:claude-extra",
                    ["claude-extra:claude-opus-5-5"],
                ),
                [],
            )
            self.assertEqual(
                orch.find_usable_in_available(
                    self.catalog,
                    orch.load_file(assets_path),
                    orch.get_now("2026-11-25T00:05:00Z"),
                    "claude-extra",
                    ["claude-extra:claude-opus-5-5"],
                ),
                [],
            )

            # Record provider+tier followup and grouped provider+group+tier followup
            for ref, needs, after in (
                ("batch-deferred-1", "provider:claude-extra/T3", "2026-11-25T00:02:00+00:00"),
                ("batch-gemini-group", "provider:antigravity/Gemini Models/T3", "2026-11-26T00:02:00+00:00"),
            ):
                add_args = _Args(
                    assets=assets_path,
                    catalog=CATALOG_PATH,
                    now="2026-11-20T21:00:00Z",
                    due=False,
                    add=True,
                    kind="resume",
                    ref=ref,
                    needs=needs,
                    after=after,
                    done=None,
                    available=None,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(orch.followups_cmd(add_args), 0)

            # At 2026-11-21: claude-extra is exhausted, and antigravity's Gemini Models group is exhausted
            # (even though primary claude AND antigravity's Claude and GPT models group are both ok!) -> neither due
            due_before = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-21T12:00:00Z",
                due=True,
                add=False,
                done=None,
                available="claude:claude-opus-5-5,claude-extra:claude-opus-5-5,antigravity:claude-opus-5-5,antigravity:gemini-4-neon",
            )
            buf_before = io.StringIO()
            with contextlib.redirect_stdout(buf_before):
                self.assertEqual(orch.followups_cmd(due_before), 0)
            _assert_none_due(self, buf_before.getvalue())

            # At 2026-11-25T00:05:00Z: claude-extra has renewed, but if only below-T3 Haiku is available on claude-extra, still not due
            due_below_tier = _Args(
                **{
                    **due_before.__dict__,
                    "now": "2026-11-25T00:05:00Z",
                    "available": "claude-extra:claude-haiku-5-5,antigravity:claude-opus-5-5,antigravity:gemini-4-neon",
                }
            )
            buf_below = io.StringIO()
            with contextlib.redirect_stdout(buf_below):
                self.assertEqual(orch.followups_cmd(due_below_tier), 0)
            _assert_none_due(self, buf_below.getvalue())

            # With T3-meeting model on claude-extra at 2026-11-25T00:05:00Z: batch-deferred-1 is due, while batch-gemini-group is still not due
            due_mid = _Args(
                **{
                    **due_before.__dict__,
                    "now": "2026-11-25T00:05:00Z",
                }
            )
            buf_mid = io.StringIO()
            with contextlib.redirect_stdout(buf_mid):
                self.assertEqual(orch.followups_cmd(due_mid), 0)
            self.assertIn(
                "followup due: [resume] batch-deferred-1 (needs provider:claude-extra/T3; usable via claude-extra)",
                buf_mid.getvalue(),
            )
            self.assertNotIn("batch-gemini-group", buf_mid.getvalue())

            # At 2026-11-26T00:05:00Z: antigravity's Gemini Models group resets -> batch-gemini-group is now due too
            due_after = _Args(**{**due_before.__dict__, "now": "2026-11-26T00:05:00Z"})
            buf_after = io.StringIO()
            with contextlib.redirect_stdout(buf_after):
                self.assertEqual(orch.followups_cmd(due_after), 0)
            self.assertIn(
                "followup due: [resume] batch-gemini-group (needs provider:antigravity/Gemini Models/T3; usable via antigravity (Gemini Models))",
                buf_after.getvalue(),
            )


class TestBelowTierStaging(unittest.TestCase):
    def _write_grouped_below_tier_assets(self, path, below_tier=None):
        pref_block = f"preferences:\n  below_tier: {below_tier}\n" if below_tier else ""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\n"
                f"{pref_block}"
                "providers:\n"
                "  - id: antigravity\n"
                "    policy: spend-first\n"
                "    status: ok\n"
                "    groups:\n"
                '      - name: "Gemini Models"\n'
                "        models: [gemini-4-neon, gemini-4-hydrogen]\n"
                "        status: exhausted\n"
                "        used_percent: 100\n"
                "        window: 5h\n"
                "        resets_at: 2026-11-21T00:13:07+00:00\n"
                "        exhausted_until: 2026-11-21T00:13:07+00:00\n"
                '      - name: "Claude and GPT models"\n'
                "        models: [claude-opus-5-5, claude-sonnet-5-5]\n"
                "        status: exhausted\n"
                "        used_percent: 100\n"
                "        window: 7d\n"
                "        resets_at: 2026-11-27T15:59:36+00:00\n"
                "        exhausted_until: 2026-11-27T15:59:36+00:00\n"
                '      - name: "Gemini 4 Argon"\n'
                "        models: [gemini-4-argon]\n"
                "        status: ok\n"
                "        used_percent: 0\n"
                "        window: 7d\n"
                "        resets_at: 2026-11-27T21:23:09+00:00\n"
            )

    def _write_multi_provider_below_tier_assets(self, path, below_tier=None):
        pref_block = f"preferences:\n  below_tier: {below_tier}\n" if below_tier else ""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\n"
                f"{pref_block}"
                "providers:\n"
                "  - id: claude\n"
                "    policy: normal\n"
                "    status: exhausted\n"
                "    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: codex\n"
                "    policy: normal\n"
                "    status: ok\n"
            )

    def test_check_validates_below_tier_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("version: 1\npreferences:\n  below_tier: skip\nproviders: []\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.check(_Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 1)
            self.assertIn("preferences.below_tier", buf.getvalue())

            for valid in ("stage", "wait", "proceed"):
                good = os.path.join(tmp, f"good-{valid}.yaml")
                with open(good, "w", encoding="utf-8") as fh:
                    fh.write(f"version: 1\npreferences:\n  below_tier: {valid}\nproviders: []\n")
                buf_ok = io.StringIO()
                with contextlib.redirect_stdout(buf_ok):
                    rc_ok = orch.check(
                        _Args(assets=good, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="")
                    )
                self.assertEqual(rc_ok, 0)

    def test_stage_default_grouped_provider_reports_staged_plan_and_group_scoped_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_grouped_below_tier_assets(assets_path)  # default is stage

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="high",
                available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                below_tier=None,
                ref="task-staged-group",
                implementer=None,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertNotIn("NO CANDIDATE", out)
            self.assertIn("STAGE (below_tier=stage)", out)
            self.assertIn("phase 1", out)
            self.assertIn("antigravity / gemini-4-argon @ high (score 51", out)
            self.assertIn("phase 2", out)
            self.assertIn("switch same child in place to antigravity / gemini-4-neon @ high", out)
            self.assertIn("2026-11-21T00:15:07+00:00", out)
            self.assertIn("high-risk work under stage takes a reviewer meeting T4", out)

            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(
                fus[0],
                {
                    "kind": "resume",
                    "ref": "task-staged-group",
                    "needs": "provider:antigravity/Gemini Models/T4",
                    "after": "2026-11-21T00:15:07+00:00",
                    "on_recovery": True,
                },
            )

            # Before Gemini Models resets: not due even though Gemini 4 Argon is ok
            fu_before = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T22:00:00Z",
                available="antigravity:claude-opus-5-5,antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                due=True,
                add=False,
                done=None,
            )
            buf_before = io.StringIO()
            with contextlib.redirect_stdout(buf_before):
                self.assertEqual(orch.followups_cmd(fu_before), 0)
            _assert_none_due(self, buf_before.getvalue())

            # After Gemini Models resets at 2026-11-21T00:13:07+00:00: follow-up is due via antigravity (Gemini Models)
            fu_after = _Args(**{**fu_before.__dict__, "now": "2026-11-21T00:16:00Z"})
            buf_after = io.StringIO()
            with contextlib.redirect_stdout(buf_after):
                self.assertEqual(orch.followups_cmd(fu_after), 0)
            self.assertIn(
                "followup due: [resume] task-staged-group (needs provider:antigravity/Gemini Models/T4; usable via antigravity (Gemini Models))",
                buf_after.getvalue(),
            )

    def test_stage_non_grouped_provider_records_tier_need_and_blocks_high_risk_below_tier_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_multi_provider_below_tier_assets(assets_path, below_tier="stage")

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="high",
                available="claude:claude-opus-5-5,codex:gpt-6-2-sol",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                below_tier=None,
                ref="task-staged-multi",
                implementer=None,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertNotIn("NO CANDIDATE", out)
            self.assertIn("STAGE (below_tier=stage)", out)
            self.assertIn("codex / gpt-6-2-sol @ xhigh (score 55.74", out)
            self.assertIn("switch same child in place to claude / claude-opus-5-5 @ xhigh", out)

            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(
                fus[0],
                {
                    "kind": "resume",
                    "ref": "task-staged-multi",
                    "needs": "T4",
                    "after": "2026-11-23T08:02:00+00:00",
                    "on_recovery": True,
                },
            )

            # At reviewer step (--implementer), below-tier reviewer is disallowed for high-risk work under stage
            rev_args = _Args(**{**sel_args.__dict__, "implementer": "gpt-6-2-sol", "ref": "rev-staged"})
            buf_rev = io.StringIO()
            with contextlib.redirect_stdout(buf_rev):
                rc_rev = orch.select(rev_args)
            self.assertEqual(rc_rev, 4)
            self.assertIn("NO REVIEWER", buf_rev.getvalue())
            self.assertIn("high-risk work under below_tier=stage takes a reviewer meeting T4", buf_rev.getvalue())

    def test_wait_mode_preserves_no_candidate_and_schedule_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_grouped_below_tier_assets(assets_path, below_tier="wait")

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="high",
                available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                below_tier=None,
                ref="task-wait-group",
                implementer=None,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 4)
            out = buf.getvalue()
            self.assertIn("NO CANDIDATE", out)
            self.assertIn("SCHEDULE_RESUME", out)
            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(
                fus[0],
                {
                    "kind": "resume",
                    "ref": "task-wait-group",
                    "needs": "provider:antigravity/Gemini Models/T4",
                    "after": "2026-11-21T00:15:07+00:00",
                    "on_recovery": True,
                },
            )

    def test_proceed_mode_runs_all_phases_provisionally_and_records_tier_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_grouped_below_tier_assets(assets_path, below_tier="proceed")

            sel_args = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                complexity="hard",
                risk="high",
                available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
                ui=False,
                fail_on_low=False,
                prefer_family=None,
                fallback=None,
                below_tier=None,
                ref="task-proceed-group",
                implementer=None,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(sel_args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertNotIn("NO CANDIDATE", out)
            self.assertIn("[provisional] (below_tier=proceed)", out)
            self.assertIn("implementer: antigravity / gemini-4-argon @ high (score 51", out)
            self.assertIn("high-risk work under proceed takes a reviewer meeting T4", out)
            self.assertNotIn("same model, fresh conversation", out)

            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(
                fus[0],
                {
                    "kind": "resume",
                    "ref": "task-proceed-group",
                    "needs": "provider:antigravity/Gemini Models/T4",
                    "after": "2026-11-21T00:15:07+00:00",
                    "on_recovery": True,
                },
            )


    def _sel_args(self, assets_path, available, **kw):
        base = dict(
            assets=assets_path,
            catalog=CATALOG_PATH,
            now="2026-11-20T21:00:00Z",
            complexity="hard",
            risk="high",
            available=available,
            ui=False,
            fail_on_low=False,
            prefer_family=None,
            fallback=None,
            below_tier=None,
            ref=None,
            implementer=None,
        )
        base.update(kw)
        return _Args(**base)

    def test_stage_without_any_tier_meeting_model_offered_keeps_no_tier_match(self):
        # Staging needs a tier-meeting target waiting on quota; with none offered, select stays exit 2.
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_multi_provider_below_tier_assets(assets_path, below_tier="stage")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(self._sel_args(assets_path, "codex:gpt-6-2-sol", ref="task-no-target"))
            self.assertEqual(rc, 2)
            self.assertIn("NO CANDIDATE", buf.getvalue())
            self.assertNotIn("STAGE", buf.getvalue())
            self.assertEqual(orch.load_file(assets_path).get("followups") or [], [])

    def test_stage_high_risk_reviewer_degrades_when_no_reviewer_tier_model_is_waiting(self):
        # Nothing at the reviewer tier is exhausted, so there is nothing to wait for: fallback order applies.
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_multi_provider_below_tier_assets(assets_path, below_tier="stage")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(
                    self._sel_args(assets_path, "codex:gpt-6-2-sol", implementer="gpt-6-2-sol", ref="rev-none")
                )
            self.assertEqual(rc, 0)
            self.assertNotIn("NO REVIEWER", buf.getvalue())
            self.assertIn("reviewer degraded", buf.getvalue())

    def test_proceed_followup_is_deduplicated_per_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            self._write_grouped_below_tier_assets(assets_path, below_tier="proceed")
            args = self._sel_args(
                assets_path, "antigravity:gemini-4-neon,antigravity:gemini-4-argon", ref="task-proceed-dup"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.select(args), 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(orch.select(args), 0)
            self.assertIn("followup already scheduled: [resume] task-proceed-dup", buf.getvalue())
            self.assertEqual(len(orch.load_file(assets_path).get("followups") or []), 1)

    def test_model_specific_limit_keeps_same_pool_models_usable(self):
        # A per-model cap (exhaust --model on an ungrouped provider) locks only that model; the rest of
        # the account's pool stays a valid fallback, and stage upgrades within the same pool at the boundary.
        with tempfile.TemporaryDirectory() as tmp:
            assets_path = os.path.join(tmp, "assets.yaml")
            with open(assets_path, "w", encoding="utf-8") as fh:
                fh.write("version: 1\nproviders:\n  - id: claude\n    policy: normal\n    status: ok\n")
            ex = _Args(
                assets=assets_path,
                catalog=CATALOG_PATH,
                now="2026-11-20T21:00:00Z",
                provider="claude",
                message="You've hit your weekly limit for Opus · resets Nov 23, 8am (UTC)",
                group=None,
                model="claude-opus-5-5",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.exhaust(ex), 0)
            avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.select(self._sel_args(assets_path, avail, complexity="standard", risk="low"))
            self.assertEqual(rc, 0)
            self.assertIn("implementer: claude / claude-sonnet-5-5", buf.getvalue())

            buf_stage = io.StringIO()
            with contextlib.redirect_stdout(buf_stage):
                rc_stage = orch.select(self._sel_args(assets_path, avail, risk="medium", ref="task-same-pool"))
            self.assertEqual(rc_stage, 0)
            out = buf_stage.getvalue()
            self.assertIn("(phase 1, low-risk only", out)
            self.assertIn("claude / claude-sonnet-5-5", out)
            self.assertIn("switch same child in place to claude / claude-opus-5-5", out)
            self.assertIn("select when the review step starts in phase 2", out)
            fus = orch.load_file(assets_path).get("followups") or []
            self.assertEqual(fus[0]["needs"], "provider:claude/claude-opus-5-5/T3")
            self.assertEqual(fus[0]["after"], "2026-11-23T08:02:00+00:00")


def _run(fn, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(args)
    return rc, buf.getvalue()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _sel(assets_path, available, **kw):
    base = dict(
        assets=assets_path,
        catalog=CATALOG_PATH,
        now="2026-11-20T21:00:00Z",
        complexity="hard",
        risk="high",
        available=available,
        ui=False,
        fail_on_low=False,
        prefer_family=None,
        fallback=None,
        below_tier=None,
        ref=None,
        implementer=None,
    )
    base.update(kw)
    return _Args(**base)


def _exh(assets_path, provider, message, group=None, model=None, now="2026-11-20T21:00:00Z"):
    return _Args(
        assets=assets_path, catalog=CATALOG_PATH, now=now, provider=provider, message=message, group=group, model=model
    )


def _fu(assets_path, now="2026-11-20T21:00:00Z", **kw):
    base = dict(
        assets=assets_path, catalog=CATALOG_PATH, now=now, available=None, due=False, add=False, done=None,
        kind=None, ref=None, needs=None, after=None, on_recovery=False,
    )
    base.update(kw)
    return _Args(**base)


def _probe(assets_path, now="2026-11-20T21:00:00Z", provider=None, init=False):
    return _Args(
        assets=assets_path, catalog=CATALOG_PATH, now=now, provider=provider,
        low_threshold=None, exhausted_threshold=None, available=None, init=init,
    )


GROUPED_AG = (
    "  - id: antigravity\n"
    "    policy: spend-first\n"
    "    status: {pst}\n"
    "{pextra}"
    "    groups:\n"
    '      - name: "Gemini Models"\n'
    "        models: [gemini-4-neon, gemini-4-hydrogen]\n"
    "        status: {gem}\n"
    "        used_percent: {gem_pct}\n"
    "        window: 7d\n"
    "        resets_at: 2026-11-21T00:13:07+00:00\n"
    '      - name: "Claude and GPT models"\n'
    "        models: [claude-opus-5-5, claude-sonnet-5-5]\n"
    "        status: exhausted\n"
    "        used_percent: 100\n"
    "        window: 7d\n"
    "        resets_at: 2026-11-27T15:59:36+00:00\n"
    "        exhausted_until: 2026-11-27T15:59:36+00:00\n"
    '      - name: "Gemini 4 Argon"\n'
    "        models: [gemini-4-argon]\n"
    "        status: {arg}\n"
    "        used_percent: {arg_pct}\n"
    "        window: 7d\n"
    "        resets_at: 2026-11-27T21:23:09+00:00\n"
)


def _grouped_ag(pst="ok", pextra="", gem="ok", gem_pct=10, arg="ok", arg_pct=0):
    return GROUPED_AG.format(pst=pst, pextra=pextra, gem=gem, gem_pct=gem_pct, arg=arg, arg_pct=arg_pct)


class TestAuditBlocking(unittest.TestCase):
    """Red→green repros for the cross-family scenario audit of v0.5.0 (blocking items 1-7)."""

    def setUp(self):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)

    # B1: stale provider-level exhaustion on grouped providers
    def test_b1_exhaust_without_group_on_grouped_provider_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n" + _grouped_ag())
            rc, out = _run(orch.exhaust, _exh(ap, "antigravity", "Individual quota reached. Resets in 3h13m7s."))
            self.assertEqual(rc, 1)
            self.assertIn("--model", out)
            self.assertIn("Gemini Models", out)
            self.assertEqual(orch.load_file(ap)["providers"][0]["status"], "ok")

    def test_b1_probe_clears_provider_lock_when_a_previously_exhausted_group_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            extra = (
                "    probe: {kind: antigravity}\n"
                "    exhausted_until: 2026-11-21T03:00:00+00:00\n"
                '    status_note: "Individual quota reached"\n'
            )
            _write(ap, "version: 1\nproviders:\n" + _grouped_ag(pst="exhausted", pextra=extra, gem="exhausted", gem_pct=100))
            fake = subprocess.CompletedProcess(args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr="")
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch("subprocess.run", return_value=fake):
                rc, out = _run(orch.probe, _probe(ap, now="2026-11-20T21:05:00Z", provider="antigravity"))
            self.assertEqual(rc, 0, out)
            self.assertIn("antigravity: ok (2/3 groups usable)", out)
            ag = orch.load_file(ap)["providers"][0]
            self.assertEqual(ag["status"], "ok")
            self.assertNotIn("exhausted_until", ag)
            rc_sel, sel_out = _run(orch.select, _sel(ap, "antigravity:gemini-4-neon", now="2026-11-20T21:05:00Z"))
            self.assertEqual(rc_sel, 0, sel_out)
            self.assertNotIn("provider exhausted", sel_out)

    def test_b1_ui_preference_counts_provider_exhaustion_on_grouped_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            extra = "    exhausted_until: 2026-11-21T03:00:00+00:00\n"
            _write(
                ap,
                "version: 1\nproviders:\n"
                + _grouped_ag(pst="exhausted", pextra=extra).replace(
                    "models: [claude-opus-5-5, claude-sonnet-5-5]\n        status: exhausted",
                    "models: [claude-opus-5-5, claude-sonnet-5-5]\n        status: ok",
                )
                + "  - id: codex\n    status: ok\n",
            )
            rc, out = _run(
                orch.select,
                _sel(ap, "antigravity:claude-sonnet-5-5,codex:gpt-6-2-sol", complexity="standard", risk="low", ui=True),
            )
            self.assertEqual(rc, 0, out)
            self.assertNotIn("not offered", out)
            self.assertIn("[provisional] preferred ui-ux-review exhausted", out)

    # B2: high-risk stage reviewer must use a tier-meeting same-model review before waiting
    def test_b2_stage_high_risk_reviewer_prefers_tier_meeting_same_model_over_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\npreferences:\n  below_tier: stage\nproviders:\n"
                "  - id: antigravity\n    status: ok\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: codex\n    status: ok\n",
            )
            avail = "antigravity:gemini-4-neon,claude:claude-opus-5-5,codex:gpt-6-2-sol"
            rc, out = _run(orch.select, _sel(ap, avail, implementer="gemini-4-neon", ref="rev-high-1"))
            self.assertEqual(rc, 0, out)
            self.assertIn("same model, fresh conversation): antigravity / gemini-4-neon", out)
            self.assertNotIn("NO REVIEWER", out)
            self.assertEqual(orch.load_file(ap).get("followups") or [], [])

    # B3: group-scoped needs only when every exhausted target sits in ONE (provider, group)
    def test_b3_needs_stays_tier_wide_when_targets_span_two_groups_of_one_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n" + _grouped_ag(gem="exhausted", gem_pct=100).replace(
                "resets_at: 2026-11-21T00:13:07+00:00\n",
                "resets_at: 2026-11-21T00:13:07+00:00\n        exhausted_until: 2026-11-21T00:13:07+00:00\n",
            ))
            avail = "antigravity:claude-opus-5-5,antigravity:gemini-4-neon,antigravity:gemini-4-argon"
            rc, out = _run(orch.select, _sel(ap, avail, ref="task-two-groups"))
            self.assertEqual(rc, 0, out)
            fus = orch.load_file(ap).get("followups") or []
            self.assertEqual(fus[0]["needs"], "T4")

    # B4: an expired provider lockout clears exactly once while its groups stay exhausted
    def test_b4_expired_provider_lock_clears_once_when_groups_stay_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            extra = (
                "    exhausted_until: 2026-11-20T20:00:00+00:00\n"
                '    status_note: "quota reached"\n'
            )
            text = "version: 1\nproviders:\n" + _grouped_ag(pst="exhausted", pextra=extra, gem="exhausted", gem_pct=100, arg="exhausted", arg_pct=100)
            text = text.replace(
                "resets_at: 2026-11-21T00:13:07+00:00\n",
                "resets_at: 2026-11-21T00:13:07+00:00\n        exhausted_until: 2026-11-21T00:13:07+00:00\n",
            ).replace(
                "resets_at: 2026-11-27T21:23:09+00:00\n",
                "resets_at: 2026-11-27T21:23:09+00:00\n        exhausted_until: 2026-11-27T21:23:09+00:00\n",
            )
            _write(ap, text)
            _, first = _run(orch.probe, _probe(ap, provider="none"))
            _, second = _run(orch.probe, _probe(ap, provider="none"))
            self.assertIn("antigravity: expired lockout cleared", first)
            self.assertNotIn("expired lockout cleared", second)
            ag = orch.load_file(ap)["providers"][0]
            self.assertEqual(ag["status"], "exhausted")
            self.assertEqual(ag["exhausted_until"], "2026-11-21T00:13:07+00:00")
            self.assertNotIn("status_note", ag)

    # B5: mid-review, every reviewer exhausted -> SCHEDULE_RESUME + resume follow-up
    def test_b5_reviewer_step_with_all_exhausted_schedules_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\nproviders:\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T08:00:00+00:00\n",
            )
            rc, out = _run(
                orch.select,
                _sel(ap, "claude:claude-opus-5-5,codex:gpt-6-2-sol", risk="low", implementer="claude-opus-5-5", ref="rev-child-1"),
            )
            self.assertEqual(rc, 4)
            self.assertIn("NO REVIEWER", out)
            self.assertIn("SCHEDULE_RESUME", out)
            self.assertIn("2026-11-22T08:02:00+00:00", out)
            fus = orch.load_file(ap).get("followups") or []
            self.assertEqual(
                fus, [{"kind": "resume", "ref": "rev-child-1", "needs": "T3", "after": "2026-11-22T08:02:00+00:00", "on_recovery": True}]
            )

    # B6: the staged plan states the per-(harness, provider) continue rule and the scheduler fallback
    def test_b6_stage_plan_names_verified_pairs_and_no_scheduler_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\nproviders:\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: codex\n    status: ok\n",
            )
            rc, out = _run(orch.select, _sel(ap, "claude:claude-opus-5-5,codex:gpt-6-2-sol", ref="t-b6"))
            self.assertEqual(rc, 0, out)
            self.assertIn("verified or observed (harness, provider) pair", out)
            self.assertIn("without a scheduler", out)
            self.assertIn("defer the new Phase 2 child", out)

    # B7: a second per-model cap on an ungrouped provider
    def test_b7_second_model_cap_on_ungrouped_provider_appends_a_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n  - id: claude\n    status: ok\n")
            rc1, _ = _run(orch.exhaust, _exh(ap, "claude", "Opus limit · resets Nov 23, 8am (UTC)", model="claude-opus-5-5"))
            rc2, out2 = _run(orch.exhaust, _exh(ap, "claude", "Sonnet limit · resets Nov 22, 8am (UTC)", model="claude-sonnet-5-5"))
            self.assertEqual((rc1, rc2), (0, 0), out2)
            cl = orch.load_file(ap)["providers"][0]
            self.assertEqual(cl["status"], "ok")
            self.assertEqual({g["name"] for g in cl["groups"]}, {"claude-opus-5-5", "claude-sonnet-5-5"})
            avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,claude:claude-haiku-5-5"
            rc, out = _run(orch.select, _sel(ap, avail, complexity="trivial", risk="low"))
            self.assertEqual(rc, 0, out)
            self.assertIn("implementer: claude / claude-haiku-5-5", out)
            # Pending follow-ups still see the uncapped model of that provider.
            _run(orch.followups_cmd, _fu(ap, add=True, kind="resume", ref="r-anth", needs="anthropic"))
            _, pend = _run(orch.followups_cmd, _fu(ap))
            self.assertIn("usable in assets via claude", pend)


class TestAuditFollowUps(unittest.TestCase):
    """Red→green repros for the audit's follow-up items."""

    def test_f1_resume_after_is_updated_when_the_task_is_exhausted_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\npreferences:\n  below_tier: wait\nproviders:\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-21T04:30:00+00:00\n"
                "followups:\n  - kind: resume\n    ref: t-stale\n    needs: T4\n    after: 2026-11-21T00:15:07+00:00\n",
            )
            rc, out = _run(orch.select, _sel(ap, "claude:claude-opus-5-5", now="2026-11-21T01:00:00Z", ref="t-stale"))
            self.assertEqual(rc, 4, out)
            self.assertIn("followup updated", out)
            fus = orch.load_file(ap)["followups"]
            self.assertEqual(fus, [{"kind": "resume", "ref": "t-stale", "needs": "T4", "after": "2026-11-21T04:32:00+00:00", "on_recovery": True}])

    def test_f2_resume_followup_waits_for_its_after_time_on_a_renewing_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n  - id: claude\n    status: low\n    used_percent: 90\n")
            _run(
                orch.followups_cmd,
                _fu(ap, add=True, kind="resume", ref="batch-renew", needs="provider:claude/T3", after="2026-11-25T00:02:00+00:00"),
            )
            _, before = _run(orch.followups_cmd, _fu(ap, due=True, available="claude:claude-opus-5-5"))
            _assert_none_due(self, before)
            _, after = _run(
                orch.followups_cmd, _fu(ap, now="2026-11-25T00:03:00Z", due=True, available="claude:claude-opus-5-5")
            )
            self.assertIn("followup due: [resume] batch-renew", after)

    def test_f2_quota_resume_is_due_early_when_its_pool_recovers_before_after(self):
        # A resume recorded while the needed pool was exhausted fires as soon as that pool is usable again,
        # even before its `after` time (early renewals happen); a renewing-provider deferral keeps waiting.
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\npreferences:\n  below_tier: wait\nproviders:\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-27T15:59:36+00:00\n",
            )
            rc, out = _run(orch.select, _sel(ap, "claude:claude-opus-5-5", ref="t-early"))
            self.assertEqual(rc, 4, out)
            fu = orch.load_file(ap)["followups"][0]
            self.assertEqual(fu["after"], "2026-11-27T16:01:36+00:00")
            self.assertIs(fu.get("on_recovery"), True)
            # Still exhausted: not due.
            _, still = _run(orch.followups_cmd, _fu(ap, now="2026-11-21T09:00:00Z", due=True, available="claude:claude-opus-5-5"))
            _assert_none_due(self, still)
            # The probe shows the pool recovered days early (status back to ok): due now.
            _run(orch.status_cmd, _Args(assets=ap, now="2026-11-21T10:00:00Z", provider="claude", state="ok"))
            _, early = _run(orch.followups_cmd, _fu(ap, now="2026-11-21T10:05:00Z", due=True, available="claude:claude-opus-5-5"))
            self.assertIn("followup due: [resume] t-early", early)
            # The manual quota-resume form can opt in too.
            rc_add, _ = _run(
                orch.followups_cmd,
                _fu(ap, add=True, kind="resume", ref="t-manual", needs="T4", after="2026-11-30T00:00:00+00:00", on_recovery=True),
            )
            self.assertEqual(rc_add, 0)
            _, manual = _run(orch.followups_cmd, _fu(ap, now="2026-11-21T10:05:00Z", due=True, available="claude:claude-opus-5-5"))
            self.assertIn("followup due: [resume] t-manual", manual)

    def test_f3_probe_leaves_a_missing_assets_file_uncreated_unless_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "sub", "assets.yaml")
            with mock.patch.object(orch, "_infer_probe", return_value=None), mock.patch("shutil.which", return_value=None):
                rc, out = _run(orch.probe, _probe(ap))
                self.assertEqual(rc, 0)
                self.assertFalse(os.path.exists(ap))
                self.assertIn("--init", out)
                _run(orch.probe, _probe(ap, init=True))
            self.assertTrue(os.path.exists(ap))

    def test_f3_check_handles_missing_assets_and_validates_pay_per_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.yaml")
            rc, out = _run(orch.check, _Args(assets=missing, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 0, out)
            self.assertIn("no assets file", out)
            bad = os.path.join(tmp, "bad.yaml")
            _write(bad, "version: 1\npreferences:\n  pay_per_use: bogus\nproviders: []\n")
            rc_bad, out_bad = _run(orch.check, _Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc_bad, 1)
            self.assertIn("preferences.pay_per_use", out_bad)
            _write(bad, "version: 1\npreferences:\n  usage_cache_ttl_seconds: -1\nproviders: []\n")
            rc_ttl, out_ttl = _run(orch.check, _Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc_ttl, 1)
            self.assertIn("preferences.usage_cache_ttl_seconds", out_ttl)

    def test_f3_wait_records_resume_without_ref_and_with_unknown_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\npreferences:\n  below_tier: wait\nproviders:\n  - id: claude\n    status: exhausted\n")
            rc, out = _run(orch.select, _sel(ap, "claude:claude-opus-5-5"))
            self.assertEqual(rc, 4, out)
            self.assertIn("reset time unknown", out)
            self.assertEqual(orch.load_file(ap)["followups"], [{"kind": "resume", "ref": "current-task", "needs": "T4", "on_recovery": True}])

    def test_f4_check_requires_numeric_scores_for_estimated_rating(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = orch.load_file(CATALOG_PATH)
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders: []\n")
            _write(
                os.path.join(tmp, "catalog.local.yaml"),
                "models:\n  - id: new-model-x\n    name: New Model X\n    family: openai\n    cost: normal\n    rating: estimated\n",
            )
            rc, out = _run(orch.check, _Args(assets=ap, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
            self.assertEqual(rc, 1, out)
            self.assertIn("new-model-x: estimated needs numeric scores", out)
            self.assertTrue(cat["models"])


class TestAuditRecheck(unittest.TestCase):
    """Red→green repros for the second audit pass on d5ad9da."""

    def setUp(self):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)

    def test_n1_expired_model_cap_resume_is_ready_without_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n  - id: claude\n    status: ok\n")
            rc_ex, _ = _run(orch.exhaust, _exh(ap, "claude", "Opus limit · resets Nov 21, 2am (UTC)", model="claude-opus-5-5"))
            self.assertEqual(rc_ex, 0)
            rc, out = _run(orch.select, _sel(ap, "claude:claude-opus-5-5,claude:claude-sonnet-5-5", ref="t-cap"))
            self.assertEqual(rc, 0, out)
            self.assertEqual(orch.load_file(ap)["followups"][0]["needs"], "provider:claude/claude-opus-5-5/T4")
            _, pend = _run(orch.followups_cmd, _fu(ap, now="2026-11-21T02:03:00Z", due=True))
            self.assertIn("followup pending: needs provider:claude/claude-opus-5-5/T4", pend)
            self.assertIn("usable in assets via claude", pend)

    def test_n2_fail_on_low_stage_keeps_its_resume_pending_while_the_pool_is_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(
                ap,
                "version: 1\nproviders:\n"
                "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: antigravity\n    status: low\n    used_percent: 90\n"
                "  - id: codex\n    status: ok\n",
            )
            avail = "claude:claude-opus-5-5,antigravity:gemini-4-neon,codex:gpt-6-2-sol"
            rc, out = _run(orch.select, _sel(ap, avail, fail_on_low=True, ref="task-low-stage"))
            self.assertEqual(rc, 0, out)
            self.assertIn("STAGE", out)
            self.assertNotIn("followup due", out)
            fu = orch.load_file(ap)["followups"][0]
            self.assertIs(fu.get("fail_on_low"), True)
            _, still = _run(orch.followups_cmd, _fu(ap, due=True, available=avail))
            _assert_none_due(self, still)
            _run(orch.status_cmd, _Args(assets=ap, now="2026-11-21T00:00:00Z", provider="antigravity", state="ok"))
            _, due = _run(orch.followups_cmd, _fu(ap, now="2026-11-21T00:05:00Z", due=True, available=avail))
            self.assertIn("followup due: [resume] task-low-stage", due)

    def test_n3_probe_derived_groups_reject_bare_exhaust_under_a_custom_provider_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders:\n" + _grouped_ag().replace("- id: antigravity", "- id: ag-work"))
            rc, out = _run(orch.exhaust, _exh(ap, "ag-work", "Individual quota reached. Resets in 1h."))
            self.assertEqual(rc, 1, out)
            self.assertIn("--model", out)

    def test_n3_probe_treats_exhausted_group_without_used_percent_as_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            extra = (
                "    probe: {kind: antigravity}\n"
                "    exhausted_until: 2026-11-21T03:00:00+00:00\n"
                '    status_note: "Individual quota reached"\n'
            )
            text = "version: 1\nproviders:\n" + _grouped_ag(pst="exhausted", pextra=extra, gem="exhausted")
            text = text.replace("        used_percent: 10\n", "")
            _write(ap, text)
            fake = subprocess.CompletedProcess(args=["agy"], returncode=0, stdout=json.dumps(self.fixture), stderr="")
            with mock.patch("shutil.which", return_value="/usr/bin/agy"), mock.patch("subprocess.run", return_value=fake):
                rc, out = _run(orch.probe, _probe(ap, now="2026-11-20T21:05:00Z", provider="antigravity"))
            self.assertEqual(rc, 0, out)
            self.assertEqual(orch.load_file(ap)["providers"][0]["status"], "ok")

    def test_n3_followups_add_refreshes_a_stale_resume_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, "version: 1\nproviders: []\n")
            _run(orch.followups_cmd, _fu(ap, add=True, kind="resume", ref="r1", needs="T3", after="2026-11-21T00:15:07+00:00"))
            rc, out = _run(
                orch.followups_cmd,
                _fu(ap, now="2026-11-21T01:00:00Z", add=True, kind="resume", ref="r1", needs="T3", after="2026-11-21T04:32:00+00:00"),
            )
            self.assertEqual(rc, 0)
            self.assertIn("followup updated", out)
            self.assertEqual(
                orch.load_file(ap)["followups"],
                [{"kind": "resume", "ref": "r1", "needs": "T3", "after": "2026-11-21T04:32:00+00:00"}],
            )
            _, same = _run(
                orch.followups_cmd,
                _fu(ap, now="2026-11-21T01:00:00Z", add=True, kind="resume", ref="r1", needs="T3", after="2026-11-21T04:32:00+00:00"),
            )
            self.assertIn("followup already scheduled", same)

class TestClaudeUsageCache(unittest.TestCase):
    T0 = datetime(2026, 11, 21, 4, 55, tzinfo=timezone.utc)
    BODY = {"five_hour": {"utilization": 31, "resets_at": "2026-11-21T09:00:00Z"}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cdir = os.path.join(tmp.name, "claude")
        os.makedirs(self.cdir)
        with open(os.path.join(self.cdir, ".credentials.json"), "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "sk-secret-token"}}, fh)
        self.tmp = tmp.name
        self.cache_dir = os.path.join(tmp.name, "usage-cache")

    def cfg(self, **kw):
        return {"config_dir": self.cdir, "_cache_dir": self.cache_dir, **kw}

    def cache_files(self):
        return [os.path.join(self.cache_dir, f) for f in sorted(os.listdir(self.cache_dir))]

    def at(self, seconds):
        return self.T0 + timedelta(seconds=seconds)

    def ok(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(self.BODY).encode()
        return resp

    def err(self, msg):
        return urllib.error.URLError(msg)

    def err429(self, retry_after=None):
        hdrs = {"Retry-After": retry_after} if retry_after else {}
        return urllib.error.HTTPError("u", 429, "Too Many", hdrs, None)

    def probe(self, seconds, effects, **kw):
        with mock.patch("orch.urllib.request.urlopen", side_effect=effects) as m:
            out = orch.probe_claude(self.cfg(**kw), self.at(seconds))
        return out, m.call_count

    def test_cache_hit_makes_no_request(self):
        (res, _), n = self.probe(0, [self.ok()])
        self.assertEqual((res["used_percent"], n), (31, 1))
        (res2, note), n2 = self.probe(299, [])
        self.assertEqual((res2, note, n2), (res, None, 0))

    def test_ttl_expiry_requests_again(self):
        self.probe(0, [self.ok()])
        (res, _), n = self.probe(301, [self.ok()])
        self.assertEqual((res["used_percent"], n), (31, 1))

    def test_429_retry_after_backs_off_and_reports_last_known(self):
        self.probe(0, [self.ok()])
        (res, note), n = self.probe(400, [self.err429("900")])
        self.assertIsNone(res)
        self.assertEqual(n, 1)
        self.assertEqual(
            note, "unverified (HTTP 429, backing off until 05:16Z); last known 31% of 5h at 04:55Z"
        )
        (res, note2), n2 = self.probe(800, [], _fresh=True)
        self.assertEqual((res, n2), (None, 0))
        self.assertIn("backing off until 05:16Z", note2)
        (res, _), n3 = self.probe(1400, [self.ok()])
        self.assertEqual((res["used_percent"], n3), (31, 1))
        (res, note), n4 = self.probe(1400 + 400, [self.err("boom")])
        self.assertEqual((res, n4), (None, 1), "backoff cleared by the success")
        self.assertNotIn("backing off", note)

    def test_429_http_date_and_default_backoff(self):
        cases = [
            ("Sat, 21 Nov 2026 05:00:00 GMT", "05:00Z"),
            ("Sat, 21 Nov 2026 05:00:00 -0000", "05:00Z"),
            (None, "05:05Z"),
            ("soon", "05:05Z"),
            ("\u00b3", "05:05Z"),
            ("9" * 30, "04:55Z"),  # capped at one day: renders the same clock time tomorrow
        ]
        for retry_after, until in cases:
            with self.subTest(retry_after=retry_after):
                shutil.rmtree(self.cache_dir, ignore_errors=True)
                (_, note), _ = self.probe(0, [self.err429(retry_after)])
                self.assertEqual(note, f"unverified (HTTP 429, backing off until {until})")

    def test_non_429_error_reports_last_known_without_backoff(self):
        self.probe(0, [self.ok()])
        for err in (self.err("boom"), urllib.error.HTTPError("u", 500, "x", {}, None)):
            (res, note), n = self.probe(400, [err])
            self.assertEqual((res, n), (None, 1))
            self.assertIn("last known 31% of 5h at 04:55Z", note)
        (res, _), n = self.probe(401, [self.ok()])
        self.assertEqual((res["used_percent"], n), (31, 1))

    def test_config_dirs_keep_separate_entries(self):
        other = os.path.join(self.tmp, "claude-work")
        shutil.copytree(self.cdir, other)
        self.probe(0, [self.ok()])
        self.probe(0, [self.err429()], config_dir=other)
        self.assertEqual(len(self.cache_files()), 2)
        (res, _), n = self.probe(10, [])
        self.assertEqual((res["used_percent"], n), (31, 0))
        (res, note), n = self.probe(10, [], config_dir=other)
        self.assertEqual((res, n), (None, 0))
        self.assertIn("backing off", note)

    def test_cached_result_past_its_reset_is_refetched(self):
        self.probe(0, [self.ok()])
        (_, _), n = self.probe(10, [self.ok()], _cache_ttl=10**6)
        self.assertEqual(n, 0)
        later = (datetime(2026, 11, 21, 9, 0, 1, tzinfo=timezone.utc) - self.T0).total_seconds()
        (res, note), n = self.probe(later, [self.err("boom")], _cache_ttl=10**6)
        self.assertEqual((res, n), (None, 1))

    def test_429_without_prior_data(self):
        (res, note), n = self.probe(0, [self.err429()])
        self.assertEqual((res, n), (None, 1))
        self.assertNotIn("last known", note)
        (res, note), n = self.probe(60, [])
        self.assertEqual((res, n), (None, 0))
        self.assertIn("backing off", note)

    def test_corrupt_cache_is_ignored(self):
        os.makedirs(self.cache_dir)
        key = orch.hashlib.sha256(self.cdir.encode()).hexdigest()[:16]
        with open(os.path.join(self.cache_dir, f"claude-{key}.json"), "w") as fh:
            fh.write("{not json")
        (res, _), n = self.probe(0, [self.ok()])
        self.assertEqual((res["used_percent"], n), (31, 1))

    def test_fresh_bypasses_ttl_only(self):
        self.probe(0, [self.ok()])
        (res, _), n = self.probe(10, [self.ok()], _fresh=True)
        self.assertEqual((res["used_percent"], n), (31, 1))

    def test_cache_outlives_probe_without_assets_file(self):
        ap = os.path.join(self.tmp, "assets.yaml")
        cfg = {"kind": "claude", "config_dir": self.cdir}
        args = _probe(ap, now="2026-11-21T04:55:00Z", provider="claude")
        args.fresh = False
        with mock.patch.object(orch, "_infer_probe", return_value=cfg), mock.patch(
            "orch.urllib.request.urlopen", side_effect=[self.ok()]
        ) as m:
            _, first = _run(orch.probe, args)
            _, second = _run(orch.probe, args)
        self.assertEqual(m.call_count, 1)
        self.assertFalse(os.path.exists(ap))
        self.assertEqual(len(self.cache_files()), 1)
        self.assertNotIn("sk-secret-token", first + second)

    def test_token_never_written_to_cache(self):
        self.probe(0, [self.ok()])
        self.probe(400, [self.err429()])
        for path in self.cache_files():
            with open(path) as fh:
                self.assertNotIn("sk-secret-token", fh.read())


CLAUDE_EXHAUSTED = (
    "version: 1\nproviders:\n"
    "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
    "  - id: codex\n    status: ok\n"
)
CLAUDE_LOW_API = (
    "version: 1\nproviders:\n"
    "  - id: claude\n    status: low\n    used_percent: 90\n    resets: 2026-11-22T00:00:00+00:00\n"
    "  - id: api\n    pay_per_use: true\n"
)


class TestSelectActionLine(unittest.TestCase):
    """Every `select` outcome ends in one `action:` line (SKILL.md Steps 3 and 7); exit codes are unchanged."""

    def sel(self, assets_text, available, rc_expected, action, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, assets_text)
            rc, out = _run(orch.select, _sel(ap, available, **kw))
        self.assertEqual(rc, rc_expected, out)
        lines = out.rstrip("\n").split("\n")
        self.assertEqual(lines[-1], action, out)
        self.assertEqual(sum(1 for x in lines if x.startswith("action: ")), 1, out)

    def test_exit0_dispatches_now(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5,codex:gpt-6-2-sol", 0,
            "action: dispatch codex/gpt-6-2-sol @ medium now; choose the reviewer when its step starts",
            complexity="hard", risk="medium",
        )

    def test_exit0_low_pool_still_dispatches(self):
        self.sel(
            CLAUDE_LOW_API, "claude:claude-opus-5-5", 0,
            "action: dispatch claude/claude-opus-5-5 @ high now; choose the reviewer when its step starts",
            complexity="hard", risk="medium",
        )

    def test_stage_plan(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5,codex:gpt-6-2-sol", 0,
            "action: stage: dispatch Phase 1 codex/gpt-6-2-sol @ xhigh now; switch to claude/claude-opus-5-5 @ xhigh "
            "at the Phase 1 boundary (earliest reset 2026-11-23T08:00:00+00:00), with a resume double check at 2026-11-23T08:02:00+00:00",
            ref="a-stage",
        )

    def test_proceed_below_tier_is_provisional(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5,codex:gpt-6-2-sol", 0,
            "action: dispatch codex/gpt-6-2-sol @ xhigh now (below T4); choose the reviewer when its step starts; "
            "mark it provisional (log --provisional)",
            below_tier="proceed", ref="a-proceed",
        )

    def _ui(self, mode, rc, action):
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            TestUiAndPreferredFallback._write_anthropic_exhausted_assets(None, ap, mode)
            avail = "claude:claude-sonnet-5-5,antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon,codex:gpt-6-2-sol"
            got, out = _run(orch.select, _sel(ap, avail, complexity="standard", risk="low", ui=True, ref="a-ui"))
        self.assertEqual(got, rc, out)
        self.assertEqual(out.rstrip("\n").split("\n")[-1], action, out)

    def test_ui_fallback_defer_holds(self):
        self._ui(
            "defer", 3,
            "action: hold until 2026-11-23T08:02:00+00:00 (preferred ui-ux-review exhausted); "
            "record a resume follow-up",
        )

    def test_ui_fallback_ask_asks_once(self):
        self._ui(
            "ask", 3,
            "action: ask the user once (preferred ui-ux-review exhausted: proceed provisionally with "
            "antigravity/gemini-4-neon @ low and queue a follow-up review, or wait until 2026-11-23T08:02:00+00:00)",
        )

    def test_ui_fallback_proceed_dispatches(self):
        self._ui(
            "proceed", 0,
            "action: dispatch antigravity/gemini-4-neon @ low now; choose the reviewer when its step starts; "
            "mark it provisional (log --provisional)",
        )

    def test_exit3_fail_on_low_reruns_without_it_and_never_holds(self):
        self.sel(
            CLAUDE_LOW_API, "claude:claude-opus-5-5", 3,
            "action: rerun select without --fail-on-low and dispatch its candidate "
            "(hold only if the user asked to preserve this pool)",
            complexity="hard", risk="medium", fail_on_low=True,
        )

    ASK_LINE = "action: ask the user once (only emergency or pay-per-use models remain, and spending them needs approval)"
    NONE_LINE = "action: no usable model: report to the user (pay-per-use under `never` and `policy: avoid` stay excluded)"

    def test_exit3_pay_per_use_under_ask_asks_the_user(self):
        self.sel(
            "version: 1\npreferences:\n  pay_per_use: ask\nproviders:\n  - id: api\n    pay_per_use: true\n",
            "api:claude-opus-5-5", 3, self.ASK_LINE, complexity="hard", risk="medium",
        )

    def test_exit3_emergency_model_asks_the_user(self):
        self.sel(CLAUDE_LOW_API, "claude:claude-fable-5-2", 3, self.ASK_LINE, complexity="hard", risk="medium")

    def test_exit3_pay_per_use_under_never_stays_excluded(self):
        self.sel(CLAUDE_LOW_API, "api:claude-opus-5-5", 3, self.NONE_LINE, complexity="hard", risk="medium")

    def test_exit3_avoid_policy_stays_excluded_even_under_ask(self):
        self.sel(
            "version: 1\npreferences:\n  pay_per_use: ask\nproviders:\n  - id: api\n    pay_per_use: true\n    policy: avoid\n",
            "api:claude-opus-5-5", 3, self.NONE_LINE, complexity="hard", risk="medium",
        )

    def test_exit4_all_tier_models_exhausted_holds_until_reset_plus_margin(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5", 4,
            "action: hold until 2026-11-23T08:02:00+00:00; follow-up recorded: schedule its wakeup",
            ref="a-hold",
        )

    def test_exit2_no_tier_match_without_a_research_note_asks_the_user(self):
        self.sel(CLAUDE_EXHAUSTED, "codex:gpt-6-2-sol", 2, "action: no tier match: ask the user", ref="a-none")

    def test_exit2_unrated_model_names_the_research_workflow(self):
        self.sel(
            CLAUDE_EXHAUSTED, "codex:gpt-6-2-sol,foo:unratedmodel", 2,
            "action: no tier match: run references/research-unrated.md or ask the user", ref="a-none",
        )

    def test_exhausted_pool_that_cannot_meet_the_tier_is_exit2_not_an_indefinite_hold(self):
        assets = "version: 1\nproviders:\n  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
        self.sel(
            assets, "claude:claude-haiku-5-5,foo:unratedmodel", 2,
            "action: no tier match: run references/research-unrated.md or ask the user",
            complexity="frontier", ref="a-frontier",
        )
        self.sel(
            assets, "claude:claude-haiku-5-5", 2, "action: no tier match: ask the user",
            complexity="frontier", ref="a-frontier",
        )

    def test_reviewer_dispatches_now(self):
        self.sel(
            CLAUDE_EXHAUSTED.replace("exhausted", "ok"), "claude:claude-opus-5-5,codex:gpt-6-2-sol", 0,
            "action: dispatch reviewer codex/gpt-6-2-sol @ medium now",
            complexity="hard", risk="medium", implementer="claude-opus-5-5",
        )

    def test_degraded_reviewer_is_reported(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5,codex:gpt-6-2-sol", 0,
            "action: dispatch reviewer codex/gpt-6-2-sol @ medium now; state the reviewer degradation in the report",
            complexity="hard", risk="medium", implementer="gpt-6-2-sol",
        )

    def test_high_risk_no_reviewer_waits_for_the_tier(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5,codex:gpt-6-2-sol", 4,
            "action: hold the review until 2026-11-23T08:02:00+00:00; follow-up recorded: schedule its wakeup",
            implementer="gpt-6-2-sol", ref="a-rev-wait",
        )

    def test_high_risk_proceed_implementer_step_holds_reviewer_and_keeps_provisional_impl(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,antigravity:gemini-4-hydrogen"
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, assets)
            got, out = _run(
                orch.select,
                _sel(
                    ap, avail, complexity="hard", risk="high",
                    below_tier="proceed", now="2026-11-21T20:00:00Z", ref="sim",
                ),
            )
            self.assertEqual(got, 0, out)
            self.assertIn("implementer: antigravity / gemini-4-hydrogen @ xhigh", out)
            self.assertIn("reviewer (T4): high-risk work under proceed takes a reviewer meeting T4", out)
            self.assertNotIn("same model, fresh conversation", out)
            self.assertEqual(
                out.rstrip("\n").split("\n")[-1],
                "action: dispatch antigravity/gemini-4-hydrogen @ xhigh now (below T4); "
                "choose the reviewer when its step starts; mark it provisional (log --provisional)",
            )

    def test_high_risk_proceed_reviewer_step_waits_for_tier_reviewer(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,antigravity:gemini-4-hydrogen"
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, assets)
            got, out = _run(
                orch.select,
                _sel(
                    ap, avail, complexity="hard", risk="high",
                    below_tier="proceed", now="2026-11-21T20:00:00Z",
                    ref="sim", implementer="gemini-4-hydrogen",
                ),
            )
            self.assertEqual(got, 4, out)
            self.assertIn("NO REVIEWER: high-risk work under below_tier=proceed takes a reviewer meeting T4", out)
            self.assertEqual(
                out.rstrip("\n").split("\n")[-1],
                "action: hold the review until 2026-11-23T08:02:00+00:00; follow-up recorded: schedule its wakeup",
            )
            fus = orch.load_file(ap).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(fus[0]["kind"], "resume")
            self.assertEqual(fus[0]["ref"], "sim")
            self.assertEqual(fus[0]["needs"], "T4")
            self.assertEqual(fus[0]["after"], "2026-11-23T08:02:00+00:00")

    def test_high_risk_proceed_reviewer_step_picks_usable_rt_reviewer(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: ok\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,antigravity:gemini-4-hydrogen"
        self.sel(
            assets, avail, 0,
            "action: dispatch reviewer claude/claude-opus-5-5 @ xhigh now",
            complexity="hard", risk="high", below_tier="proceed",
            now="2026-11-21T20:00:00Z", implementer="gemini-4-hydrogen",
        )

    def test_medium_risk_proceed_reviewer_step_keeps_fallback_order(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,antigravity:gemini-4-hydrogen"
        self.sel(
            assets, avail, 0,
            "action: dispatch reviewer antigravity/gemini-4-hydrogen @ high now; state the reviewer degradation in the report",
            complexity="hard", risk="medium", below_tier="proceed",
            now="2026-11-21T20:00:00Z", implementer="gemini-4-hydrogen",
        )

    def test_high_risk_proceed_tier_meeting_implementer_uses_fallback_reviewer(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,antigravity:gemini-4-hydrogen"
        self.sel(
            assets, avail, 0,
            "action: dispatch reviewer antigravity/gemini-4-hydrogen @ xhigh now; state the reviewer degradation in the report",
            complexity="standard", risk="high", below_tier="proceed",
            now="2026-11-21T20:00:00Z", implementer="gemini-4-hydrogen",
        )

    def test_high_risk_proceed_upgrades_followup_from_t3_to_t4_and_uses_in_place_action(self):
        assets = (
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: antigravity\n    status: ok\n"
        )
        avail = "claude:claude-opus-5-5,claude:claude-sonnet-5-5,antigravity:gemini-3-8-flash"
        with tempfile.TemporaryDirectory() as tmp:
            ap = os.path.join(tmp, "assets.yaml")
            _write(ap, assets)
            got1, out1 = _run(
                orch.select,
                _sel(
                    ap, avail, complexity="standard", risk="high",
                    below_tier="proceed", now="2026-11-21T20:00:00Z", ref="sim",
                ),
            )
            self.assertEqual(got1, 0, out1)
            self.assertIn("followup recorded: [resume] sim (needs T3, after 2026-11-23T08:02:00+00:00)", out1)

            got2, out2 = _run(
                orch.select,
                _sel(
                    ap, avail, complexity="standard", risk="high",
                    below_tier="proceed", now="2026-11-21T20:00:00Z",
                    ref="sim", implementer="gemini-3-8-flash",
                ),
            )
            self.assertEqual(got2, 4, out2)
            self.assertIn("followup updated: [resume] sim (needs T4, after 2026-11-23T08:02:00+00:00); reschedule its wakeup", out2)
            self.assertEqual(
                out2.rstrip("\n").split("\n")[-1],
                "action: hold the review until 2026-11-23T08:02:00+00:00; follow-up in place: schedule its wakeup",
            )
            fus = orch.load_file(ap).get("followups") or []
            self.assertEqual(len(fus), 1)
            self.assertEqual(fus[0]["needs"], "T4")

    def test_no_reviewer_exhausted_holds_the_review(self):
        self.sel(
            CLAUDE_EXHAUSTED, "claude:claude-opus-5-5", 4,
            "action: hold the review until 2026-11-23T08:02:00+00:00; follow-up recorded: schedule its wakeup",
            complexity="hard", risk="medium", implementer="claude-opus-5-5", ref="a-rev-none",
        )

    def test_no_reviewer_low_pool_reruns_without_fail_on_low(self):
        self.sel(
            CLAUDE_LOW_API, "claude:claude-opus-5-5", 3,
            "action: rerun select without --fail-on-low and dispatch its reviewer "
            "(hold only if the user asked to preserve this pool)",
            complexity="hard", risk="medium", implementer="claude-opus-5-5", fail_on_low=True,
        )

    def test_no_reviewer_gated_splits_by_cause(self):
        self.sel(
            "version: 1\npreferences:\n  pay_per_use: ask\nproviders:\n  - id: api\n    pay_per_use: true\n",
            "api:claude-opus-5-5", 3, self.ASK_LINE,
            complexity="hard", risk="medium", implementer="claude-opus-5-5",
        )
        self.sel(
            CLAUDE_LOW_API, "api:claude-opus-5-5", 3, self.NONE_LINE,
            complexity="hard", risk="medium", implementer="claude-opus-5-5",
        )

    def test_no_reviewer_no_tier_match(self):
        self.sel(
            CLAUDE_LOW_API, "foo:x", 2,
            "action: no tier match: run references/research-unrated.md or ask the user",
            complexity="hard", risk="medium", implementer="claude-opus-5-5",
        )

    def test_unknown_implementer_asks_for_a_fix(self):
        self.sel(
            CLAUDE_LOW_API, "codex:gpt-6-2-sol", 1,
            "action: fix --implementer (nope is not in the catalog; pass the implementing <model-id> as discovery lists it) and rerun select; dispatch only the reviewer its action line names",
            implementer="nope",
        )


def _fail(message, ref="item-1", ledger=None, known=None, assets=None, full=True):
    return _Args(
        message=message, ref=ref, ledger=ledger, known=known, assets=assets, now="2026-11-20T21:00:00Z", full=full
    )


def _log_failure(ledger, ref, fclass, outcome="failed"):
    return _Args(
        assets=None, ledger=ledger, now="2026-11-20T21:00:00Z", event="complete", complexity="hard", risk="low",
        ui=False, surface="subagent", provider="codex", model="gpt-6-2-sol", effort="high", reviewer=None,
        outcome=outcome, findings=None, repo="demo", provisional=False, ref=ref, failure_class=fclass,
    )


class TestFailureCommand(unittest.TestCase):
    """`orch.py failure` maps a child's error text to class, retries and one action (references/supervision.md)."""

    def run_failure(self, args):
        rc, out = _run(orch.failure_cmd, args)
        lines = out.rstrip("\n").split("\n")
        self.assertTrue(lines[-1].startswith("action: "), out)
        return rc, {k: v for k, _, v in (ln.partition(": ") for ln in lines)}, out

    def ledger(self, tmp, entries):
        path = os.path.join(tmp, "ledger.jsonl")
        for ref, fclass, *outcome in entries:
            _run(orch.log_cmd, _log_failure(path, ref, fclass, *outcome))
        return path

    def test_quota_parses_reset_and_points_to_exhaust(self):
        rc, f, _ = self.run_failure(_fail("You've hit your weekly limit \u00b7 resets Nov 23, 3am (America/New_York)"))
        self.assertEqual(rc, 0)
        self.assertEqual(f["class"], "quota")
        self.assertEqual(f["resets"], "2026-11-23T03:00:00-05:00")
        self.assertEqual(f["retries"], "0/2")
        self.assertEqual(f["log"], "--event complete --outcome quota --failure-class quota --ref item-1")
        self.assertEqual(
            f["action"],
            "quota (resets 2026-11-23T03:00:00-05:00): if the limit is the model provider's, run orch.py exhaust <provider> \"<error text>\" "
            "(add --model or --group per references/quota-errors.md) and follow the quota order in references/harnesses.md; "
            "otherwise treat it as infra: rerun once",
        )

    def test_compact_output_keeps_the_log_flags_and_the_action(self):
        rc, out = _run(orch.failure_cmd, _fail("You've hit your weekly limit \u00b7 resets Nov 23, 3am (America/New_York)", full=False))
        lines = out.rstrip("\n").split("\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[0], "log: --event complete --outcome quota --failure-class quota --ref item-1")
        self.assertTrue(lines[1].startswith("action: quota (resets 2026-11-23T03:00:00-05:00): "), out)

    def test_quota_without_a_reset_time(self):
        _, f, _ = self.run_failure(_fail("429 Too Many Requests: rate limit exceeded"))
        self.assertEqual((f["class"], f["resets"]), ("quota", "unknown"))

    def test_infra_signatures_get_one_rerun(self):
        for text in (
            "fetch failed: ETIMEDOUT 10.0.0.1:443", "read ECONNRESET", "HTTP 502 Bad Gateway", "503 Service Unavailable",
            "504 gateway timeout", "The runner lost communication with the server",
        ):
            _, f, _ = self.run_failure(_fail(text))
            self.assertEqual(f["class"], "infra", text)
            self.assertEqual(
                f["action"],
                "rerun the failed job or command once (references/verification.md); "
                "if it fails again, diagnose it as a real failure",
            )

    def test_context_cap_starts_a_narrower_replacement(self):
        for text in ("prompt is too long: 250001 tokens", "context length exceeded", "Reached maximum context", "stopped: max turns reached"):
            _, f, _ = self.run_failure(_fail(text))
            self.assertEqual(f["class"], "context-cap", text)
        self.assertEqual(f["log"], "--event complete --outcome failed --failure-class context-cap --ref item-1")
        self.assertEqual(
            f["action"],
            "confirm the old child stopped (references/supervision.md), then start a replacement with the partial "
            "results (branch, commit, last completed step) and a narrower scope covering only the remaining work",
        )

    def test_tool_error_retries_once_then_switches_model(self):
        for text in ("InputValidationError: missing field", "tool error: Edit failed", "spawn git ENOENT"):
            _, f, _ = self.run_failure(_fail(text))
            self.assertEqual((f["class"], f["action"]), ("tool", "retry once on the same model"), text)
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool")])
            _, f, _ = self.run_failure(_fail("tool error: boom", ledger=led))
            self.assertEqual(f["retries"], "1/2")
            self.assertEqual(
                f["action"], "rerun select with the failing model removed from --available and dispatch its candidate"
            )

    def test_unknown_retries_once(self):
        _, f, _ = self.run_failure(_fail("something odd happened"))
        self.assertEqual((f["class"], f["retries"], f["action"]), ("unknown", "0/2", "retry once"))
        self.assertEqual(f["log"], "--event complete --outcome failed --failure-class unknown --ref item-1")

    def test_two_failed_retries_stop_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool"), ("item-1", "unknown")])
            for text, cls in (("weird", "unknown"), ("tool error: x", "tool"), ("context length exceeded", "context-cap")):
                _, f, _ = self.run_failure(_fail(text, ledger=led))
                self.assertEqual((f["class"], f["retries"]), (cls, "2/2"))
                self.assertEqual(
                    f["action"],
                    "stop retrying item-1 and report it to the user with the evidence "
                    "(error text, last messages, ledger lines)",
                )

    def test_only_retry_classes_for_the_same_ref_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(
                tmp,
                [("item-1", "quota"), ("item-1", "infra"), ("item-1", None), ("other", "tool"), ("other", "tool"), ("item-1", "unknown")],
            )
            _, f, _ = self.run_failure(_fail("weird", ledger=led))
            self.assertEqual((f["retries"], f["action"]), ("1/2", "retry once"))
            _, f, _ = self.run_failure(_fail("ETIMEDOUT", ledger=led))
            self.assertEqual(f["retries"], "1/2")
            self.assertTrue(f["action"].startswith("rerun the failed job"))

    def test_quota_and_infra_never_stop_at_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool"), ("item-1", "tool")])
            _, f, _ = self.run_failure(_fail("rate limit reached, try again in 2h", ledger=led))
            self.assertEqual((f["class"], f["retries"]), ("quota", "2/2"))
            self.assertTrue(f["action"].startswith("quota (resets "))
            self.assertIn("if the limit is the model provider's", f["action"])

    def test_known_failures_win_over_every_class(self):
        known = ["eslint exploded=>run npm ci once, then rerun", "ETIMEDOUT=>never reached"]
        _, f, _ = self.run_failure(_fail("prompt is too long; eslint exploded", known=known))
        self.assertEqual((f["class"], f["known"], f["action"]), ("infra", "eslint exploded", "run npm ci once, then rerun"))
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool"), ("item-1", "tool")])
            _, f, _ = self.run_failure(_fail("eslint exploded", ledger=led, known=known))
            self.assertEqual(f["action"], "run npm ci once, then rerun")

    def test_bare_numbers_are_not_http_statuses(self):
        for text in ("assert 1 == 2 at line 504", "expected 502 got 200", "AssertionError: 503 != 200"):
            _, f, _ = self.run_failure(_fail(text))
            self.assertEqual(f["class"], "unknown", text)
        for text in ("HTTP 502", "HTTP/1.1 503 upstream", "status: 504", "status code 502", "Bad Gateway", "Service Unavailable"):
            _, f, _ = self.run_failure(_fail(text))
            self.assertEqual(f["class"], "infra", text)

    def test_context_cap_wins_over_infra(self):
        _, f, _ = self.run_failure(_fail("context length exceeded after HTTP 503 retries"))
        self.assertEqual(f["class"], "context-cap")

    def test_third_party_rate_limit_is_quota_with_an_infra_escape(self):
        _, f, _ = self.run_failure(_fail("Rate limited by GitHub API"))
        self.assertEqual(f["class"], "quota")
        self.assertIn("otherwise treat it as infra: rerun once", f["action"])

    def test_tool_switches_model_only_after_a_tool_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool"), ("item-1", "unknown")])
            _, f, _ = self.run_failure(_fail("tool error: boom", ledger=led))
            self.assertEqual((f["retries"], f["action"][:19]), ("2/2", "stop retrying item-"))
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "unknown")])
            _, f, _ = self.run_failure(_fail("tool error: boom", ledger=led))
            self.assertEqual((f["retries"], f["action"]), ("1/2", "retry once on the same model"))

    def test_completion_ok_restarts_the_count_for_that_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self.ledger(tmp, [("item-1", "tool"), ("item-1", "unknown"), ("item-1", None, "ok")])
            _, f, _ = self.run_failure(_fail("weird", ledger=led))
            self.assertEqual((f["retries"], f["action"]), ("0/2", "retry once"))
        with tempfile.TemporaryDirectory() as tmp:  # an infra rerun that passes is not a completion
            led = self.ledger(tmp, [("item-1", "unknown"), ("item-1", "infra", "ok"), ("item-1", "tool")])
            self.assertEqual(self.run_failure(_fail("weird", ledger=led))[1]["retries"], "2/2")

    def test_known_hit_logs_ok_or_failed_like_generic_infra(self):
        _, f, _ = self.run_failure(_fail("eslint exploded", known=["eslint exploded=>run npm ci"]))
        self.assertEqual(f["log"], "--event complete --outcome <ok|failed> --failure-class infra --ref item-1")

    def test_ref_is_required(self):
        rc, out = _run(orch.failure_cmd, _fail("x", ref=None))
        self.assertEqual(rc, 1)
        self.assertEqual(out.rstrip("\n").split("\n")[-1], "action: rerun failure with --ref <item>")

    def test_ref_slug_keeps_every_segment_of_a_relative_label_and_basenames_a_path(self):
        self.assertEqual(orch._clean_ref("PR-42/part a"), "PR-42-part-a")
        self.assertEqual(orch._clean_ref("/home/me/private/item-9/"), "item-9")
        self.assertEqual(orch._clean_ref("~/work/item-9"), "item-9")
        self.assertEqual(orch._clean_ref("../x/item-9"), "item-9")

    def test_known_pattern_must_be_regex_arrow_action(self):
        rc, out = _run(orch.failure_cmd, _fail("x", known=["no arrow"]))
        self.assertEqual(rc, 1)
        self.assertTrue(out.rstrip("\n").split("\n")[-1].startswith("action: fix the --known pattern"))
        rc, _ = _run(orch.failure_cmd, _fail("x", known=["(unclosed=>do it"]))
        self.assertEqual(rc, 1)

    def test_log_records_ref_and_class_without_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = os.path.join(tmp, "ledger.jsonl")
            rc, _ = _run(orch.log_cmd, _log_failure(led, "/home/me/private/repo/item-9", "tool"))
            self.assertEqual(rc, 0)
            with open(led, encoding="utf-8") as fh:
                entry = json.loads(fh.read())
            self.assertEqual((entry["ref"], entry["failure_class"]), ("item-9", "tool"))
            self.assertNotIn("/home/me", json.dumps(entry))
            rc, out = _run(orch.log_cmd, _log_failure(led, "x", "bogus"))
            self.assertEqual(rc, 1)
            self.assertIn("invalid failure class", out)


GIT_CFG = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=T", "-c", "user.email=t@example.com"]


def _git(root, *argv):
    p = subprocess.run(["git", *GIT_CFG, *argv], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert p.returncode == 0, p.stdout
    return p.stdout


def _scope(root, **kw):
    base = dict(repo=root, base="main", max_files=None, max_lines=None, expect=None, full=True)
    base.update(kw)
    rc, out = _run(orch.scope_cmd, _Args(**base))
    lines = out.rstrip("\n").split("\n")
    return rc, {k: v for k, _, v in (ln.partition(": ") for ln in lines)}, lines


def _hist(ledger, assets=None, ref=None, last=None, full=True, role=None):
    return _Args(ledger=ledger, assets=assets, ref=ref, last=last, now="2026-11-20T21:00:00Z", full=full, role=role)


class TestHistoryCommand(unittest.TestCase):
    """`orch.py history` answers which-model / which-reviewer / outcome / follow-up questions from the ledger and assets."""

    ENTRIES = [
        {"event": "select", "ref": "PR-1", "provider": "claude", "model": "claude-opus-5-5", "effort": "high", "reviewer": None, "provisional": True},
        {"event": "select", "provider": "codex", "model": "gpt-6-2-sol", "effort": "low"},  # no ref: not listed, counted
        {"event": "complete", "ref": "PR-1", "provider": "claude", "model": "claude-opus-5-5", "effort": "high",
         "reviewer": "codex:gpt-6-2-sol", "outcome": "ok", "findings": 2},
        {"event": "select", "ref": "PR-2", "provider": "codex", "model": "gpt-6-2-sol", "effort": "high", "reviewer": None},
        {"event": "complete", "ref": "PR-2", "provider": "codex", "model": "gpt-6-2-sol", "effort": "high", "outcome": "failed",
         "failure_class": "context-cap"},
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = os.path.join(tmp.name, "ledger.jsonl")
        self.assets = os.path.join(tmp.name, "assets.yaml")
        self.write_ledger(self.ENTRIES)
        _write(self.assets, "version: 1\nproviders:\n  - id: claude\nfollowups:\n  - kind: resume\n    ref: PR-2\n    needs: T3\n"
                            "  - kind: ui-review\n    ref: PR-9\n    needs: any\n")

    def write_ledger(self, entries):
        _write(self.ledger, "".join(json.dumps(e) + "\n" for e in entries) + "not json\n[1]\n")

    def history(self, **kw):
        rc, out = _run(orch.history_cmd, _hist(self.ledger, **{"assets": self.assets, **kw}))
        return rc, out.rstrip("\n").split("\n")

    def test_one_line_per_ref_most_recent_first_with_a_summary(self):
        rc, lines = self.history()
        self.assertEqual(rc, 0)
        self.assertEqual(lines, [
            "PR-2: codex:gpt-6-2-sol @ high; reviewer none; failed (context-cap); pending resume",
            "PR-1: claude:claude-opus-5-5 @ high; reviewer codex:gpt-6-2-sol; ok, 2 finding(s); provisional",
            "PR-9: no ledger entry; pending ui-review",
            "ok: 3 ref(s); 2 pending follow-up(s); 1 ledger entry has no ref (log with --ref to track them)",
        ])

    def test_other_role_select_keeps_the_implementer_attempt(self):
        self.write_ledger([
            {"event": "select", "ref": "PR-42", "provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
            {"event": "complete", "ref": "PR-42", "provider": "claude", "model": "claude-opus-5-5", "effort": "high",
             "outcome": "ok", "findings": 1},
            {"event": "select", "ref": "PR-42", "role": "reviewer", "provider": "codex", "model": "gpt-6-2-sol", "effort": "high"},
        ])
        _, lines = self.history(ref="PR-42")
        self.assertEqual(lines, [
            "PR-42: codex:gpt-6-2-sol @ high; reviewer none; open; role reviewer",
            "PR-42: claude:claude-opus-5-5 @ high; reviewer none; ok, 1 finding(s)",
            "ok: 1 ref(s); 0 pending follow-up(s)",
        ])
        _, lines = self.history(ref="PR-42", role="implementer")
        self.assertEqual(lines[0], "PR-42: claude:claude-opus-5-5 @ high; reviewer none; ok, 1 finding(s)")
        self.assertEqual(lines[-1], "ok: 1 ref(s); 0 pending follow-up(s)")

    def test_last_counts_refs_not_role_lines(self):
        self.write_ledger([
            {"event": "select", "ref": "PR-41", "provider": "codex", "model": "gpt-6-2-sol", "effort": "low"},
            {"event": "select", "ref": "PR-42", "provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
            {"event": "select", "ref": "PR-42", "role": "reviewer", "provider": "codex", "model": "gpt-6-2-sol", "effort": "high"},
        ])
        _, lines = self.history(last=1)
        self.assertEqual([l.split(";")[0] for l in lines[:-1]], ["PR-42: codex:gpt-6-2-sol @ high", "PR-42: claude:claude-opus-5-5 @ high"])
        self.assertTrue(lines[-1].startswith("ok: 1 ref(s);"))
        _, lines = self.history(last=2)
        self.assertEqual(len(lines) - 1, 3)
        self.assertTrue(lines[-1].startswith("ok: 2 ref(s);"))

    def test_ref_filter_and_last(self):
        _, lines = self.history(ref="PR-1")
        self.assertEqual([l.split(":")[0] for l in lines], ["PR-1", "ok"])
        self.assertEqual(lines[-1], "ok: 1 ref(s); 0 pending follow-up(s)")
        _, lines = self.history(last=1)
        self.assertEqual(lines[0].split(":")[0], "PR-2")
        self.assertEqual(lines[-1].split(";")[0], "ok: 1 ref(s)")

    def test_open_item_has_no_outcome_yet(self):
        self.write_ledger(self.ENTRIES[:1])
        _, lines = self.history()
        self.assertEqual(lines[0], "PR-1: claude:claude-opus-5-5 @ high; reviewer none; open; provisional")

    def test_a_new_select_starts_a_clean_attempt(self):
        retry = {"event": "select", "ref": "PR-2", "provider": "claude", "model": "claude-sonnet-5-5", "effort": "low"}
        self.write_ledger(self.ENTRIES + [retry])
        _, lines = self.history(ref="PR-2")
        self.assertEqual(lines[0], "PR-2: claude:claude-sonnet-5-5 @ low; reviewer none; open; pending resume")
        self.write_ledger(self.ENTRIES + [dict(retry, reviewer="codex:gpt-6-2-sol")])  # a select may carry its own reviewer
        self.assertIn("reviewer codex:gpt-6-2-sol; open", self.history(ref="PR-2")[1][0])

    def test_last_counts_pending_follow_ups_only_for_the_shown_refs(self):
        _, lines = self.history(last=1)  # PR-2 only; PR-9's follow-up is not shown
        self.assertEqual(lines[-1], "ok: 1 ref(s); 1 pending follow-up(s); 1 ledger entry has no ref (log with --ref to track them)")

    def test_ledger_refs_are_cleaned_when_read(self):
        self.write_ledger([{"event": "select", "ref": "/home/someone/work/PR-1", "provider": "p", "model": "m", "effort": "high"}])
        _, lines = self.history()
        self.assertTrue(lines[0].startswith("PR-1: p:m @ high"), lines)
        self.assertNotIn("/home", "\n".join(lines))

    def test_provisional_flag_help_says_when_to_use_it(self):
        sub = next(a for a in orch.build_parser()._actions if a.dest == "cmd").choices["log"]
        text = next(a.help for a in sub._actions if "--provisional" in a.option_strings)
        self.assertIn("below the required tier", text)

    def test_unknown_ref_is_an_error_with_an_action(self):
        rc, lines = self.history(ref="PR-404")
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], "error: no ledger entry or follow-up for ref PR-404")
        self.assertRegex(lines[-1], r"^error: 1 problem\(s\); action: ")

    def test_missing_ledger_and_assets_is_an_empty_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = _run(orch.history_cmd, _hist(os.path.join(tmp, "none.jsonl"), assets=os.path.join(tmp, "none.yaml")))
        self.assertEqual((rc, out), (0, "ok: 0 ref(s); 0 pending follow-up(s)\n"))

    def test_compact_caps_the_list_and_full_shows_all(self):
        self.write_ledger([{"event": "select", "ref": "item-%02d" % i, "provider": "p", "model": "m", "effort": "high"} for i in range(25)])
        _, lines = self.history(full=False)
        self.assertEqual(len(lines), 12)  # 10 refs, the `… N more` line, the summary
        self.assertEqual(lines[10], "… 17 more (rerun with --full)")
        _, lines = self.history(full=True)
        self.assertEqual(len(lines), 28)  # 25 refs, the PR-2 and PR-9 follow-ups (no ledger entry), then the summary

    def test_ledger_privacy_paths_in_refs_are_already_stripped(self):
        self.write_ledger([{"event": "select", "ref": "PR-1", "provider": "p", "model": "m", "effort": "high"}])
        _, lines = self.history(ref="/home/someone/work/PR-1")
        self.assertTrue(lines[0].startswith("PR-1: "), lines)


class TestScopeCommand(unittest.TestCase):
    """`orch.py scope` compares the diff to the brief's budget and flags manifests and strays."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        _git(self.root, "init", "-q", "-b", "main")
        self.put("src/app.py", "print(1)\n")
        self.put(".gitignore", "ignored/\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        _git(self.root, "checkout", "-q", "-b", "feature")

    def put(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_compact_output_labels_each_flagged_file_and_caps_the_list(self):
        for rel in ("package.json", "x.orig", "src/ok.py", "other/a.py"):
            self.put(rel, "1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "w")
        base = dict(repo=self.root, base="main", max_files=None, max_lines=None, expect=["src/**"], full=False)
        rc, out = _run(orch.scope_cmd, _Args(**base))
        lines = out.rstrip("\n").split("\n")
        self.assertEqual((rc, len(lines)), (0, 1), out)
        self.assertIn("other/a.py (outside)", lines[0])
        self.assertIn("package.json (manifest, outside)", lines[0])
        self.assertIn("x.orig (outside, stray)", lines[0])
        self.assertNotIn("src/ok.py", lines[0])
        self.assertNotIn("more (rerun", lines[0])
        for i in range(8):
            self.put("other/f%d.py" % i, "1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "more")
        _, out = _run(orch.scope_cmd, _Args(**base))
        self.assertIn("\u2026 6 more (rerun with --full)", out)

    def test_clean_diff_within_budget_is_ok(self):
        self.put("src/app.py", "print(1)\nprint(2)\n")
        self.put("src/new.py", "a\nb\nc\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "work")
        rc, f, lines = _scope(self.root, max_files=3, max_lines=10, expect=["src/**"])
        self.assertEqual(rc, 0)
        self.assertEqual((f["files"], f["lines"]), ("2/3", "4/10"))
        self.assertEqual((f["manifests/lockfiles"], f["outside --expect"], f["stray"]), ("none", "none", "none"))
        self.assertEqual(lines[-1], "action: ok")

    def test_counts_committed_staged_unstaged_and_untracked_but_not_ignored(self):
        self.put("src/c.py", "1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "w")
        self.put("src/s.py", "1\n2\n")
        _git(self.root, "add", "src/s.py")
        self.put("src/app.py", "print(1)\nprint(3)\n")
        self.put("src/u.py", "x\ny\nz")
        self.put("ignored/skip.py", "nope\n")
        _, f, _ = _scope(self.root)
        self.assertEqual((f["files"], f["lines"]), ("4", "7"))

    def test_manifests_and_lockfiles_are_listed_and_trimmed(self):
        for rel in ("package.json", "web/bun.lock", "bun.lockb", "requirements-dev.txt", "go.sum", "Gemfile.lock", "composer.json", "Cargo.toml"):
            self.put(rel, "{}\n")
        self.put("src/ok.py", "1\n")
        _, f, lines = _scope(self.root)
        self.assertEqual(
            f["manifests/lockfiles"],
            "Cargo.toml, Gemfile.lock, bun.lockb, composer.json, go.sum, package.json, requirements-dev.txt, web/bun.lock",
        )
        self.assertEqual(
            lines[-1],
            "action: trim: ask the implementer to justify or drop: Cargo.toml, Gemfile.lock, bun.lockb, composer.json, "
            "go.sum, package.json, requirements-dev.txt, web/bun.lock",
        )

    def test_files_outside_expect_globs_are_flagged(self):
        self.put("src/app.py", "print(2)\n")
        self.put("src/deep/x.py", "1\n")
        self.put("docs/readme.md", "1\n")
        self.put("top.py", "1\n")
        _, f, lines = _scope(self.root, expect=["src/*.py", "docs/"])
        self.assertEqual(f["outside --expect"], "src/deep/x.py, top.py")
        self.assertEqual(lines[-1], "action: trim: ask the implementer to justify or drop: src/deep/x.py, top.py")
        _, f, lines = _scope(self.root, expect=["src/**", "docs/**", "*.py"])
        self.assertEqual((f["outside --expect"], lines[-1]), ("none", "action: ok"))

    def test_stray_files_are_flagged(self):
        for rel in ("a.orig", "b.rej", "run.log", ".DS_Store", "c.tmp", "scratch/n.py", "pkg/tmp/m.py", "d.bak"):
            self.put(rel, "1\n")
        self.put("src/keep.py", "1\n")
        _, f, lines = _scope(self.root)
        self.assertEqual(f["stray"], ".DS_Store, a.orig, b.rej, c.tmp, d.bak, pkg/tmp/m.py, run.log, scratch/n.py")
        self.assertNotIn("keep.py", lines[-1])
        self.assertTrue(lines[-1].startswith("action: trim: ask the implementer to justify or drop: .DS_Store, a.orig"))

    def test_over_budget_names_the_largest_files(self):
        self.put("src/big.py", "x\n" * 50)
        self.put("src/mid.py", "x\n" * 5)
        self.put("src/small.py", "x\n")
        _, f, lines = _scope(self.root, max_files=2, max_lines=40)
        self.assertEqual((f["files"], f["lines"]), ("3/2", "56/40"))
        self.assertEqual(
            lines[-1],
            "action: trim: ask the implementer to justify or drop: src/big.py, src/mid.py, src/small.py; "
            "over budget: files 3>2, lines 56>40",
        )

    def test_binary_files_count_as_a_file_with_no_lines(self):
        with open(os.path.join(self.root, "img.png"), "wb") as fh:
            fh.write(b"\x89PNG\0\0data\n\n")
        _, f, _ = _scope(self.root)
        self.assertEqual((f["files"], f["lines"]), ("1", "0"))

    def test_pure_rename_is_one_file_with_no_lines(self):
        _git(self.root, "mv", "src/app.py", "src/main.py")
        _, f, lines = _scope(self.root, expect=["src/**"])
        self.assertEqual((f["files"], f["lines"], f["outside --expect"]), ("1", "0", "none"))

    def test_rename_with_an_edit_counts_only_the_edit(self):
        _git(self.root, "mv", "src/app.py", "src/main.py")
        self.put("src/main.py", "print(1)\nprint(2)\n")
        _, f, _ = _scope(self.root)
        self.assertEqual((f["files"], f["lines"]), ("1", "1"))

    def test_bare_expect_path_means_that_file_or_directory(self):
        self.put("src/deep/x.py", "1\n")
        self.put("README.md", "1\n")
        self.put("srcother/y.py", "1\n")
        _, f, _ = _scope(self.root, expect=["src", "README.md"])
        self.assertEqual(f["outside --expect"], "srcother/y.py")

    def test_missing_base_errors_with_an_action(self):
        rc, f, lines = _scope(self.root, base="origin/nope")
        self.assertEqual(rc, 1)
        self.assertIn("cannot find a merge-base with origin/nope", f["error"])
        self.assertEqual(lines[-1], "action: fix the --base or --repo argument and rerun scope")

    def test_not_a_repository_errors(self):
        with tempfile.TemporaryDirectory() as plain:
            rc, _, lines = _scope(plain)
        self.assertEqual(rc, 1)
        self.assertEqual(lines, ["error: not a git repository", "action: run scope inside the target repo"])


def _run_orch(fn, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(args)
    return rc, buf.getvalue()


class TestRepoPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.assets = os.path.join(self.tmp.name, "assets.yaml")

    def put(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def write_assets(self, text):
        with open(self.assets, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_policy(self, check=False, full=True, assets=None):
        args = _Args(
            repo=self.repo,
            check=check,
            full=full,
            assets=assets or self.assets,
            catalog=CATALOG_PATH,
        )
        return _run_orch(orch.policy_cmd, args)

    def test_no_policy_returns_ok_defaults(self):
        self.put("AGENTS.md", "# AGENTS\n\n- Keep changes small.\n")
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ok: no policy found (defaults apply)")

    def test_locates_dot_agents_file_before_agents_md(self):
        self.put(".agents/orchestration.md", "# Orchestration\n- Land: `bun run land <pr>`\n")
        self.put("AGENTS.md", "# AGENTS\n\n## Orchestration\n- Land: `make land`\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 0)
        self.assertIn("policy: .agents/orchestration.md", out)
        self.assertIn("Land: `bun run land <pr>`", out)
        self.assertIn("ok: policy valid (1 field)", out)

    def test_unknown_and_duplicate_keys_error(self):
        self.put(
            "AGENTS.md",
            "# AGENTS\n\n## Orchestration\n"
            "- Implement: build-it\n"
            "- Implement: build-it-again\n"
            "- Mystery: something\n",
        )
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("duplicate policy key 'Implement'", out)
        self.assertIn("unknown policy key 'Mystery'", out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("error: 2 problem(s); action: "))

    def test_models_typed_parsing_and_validation(self):
        self.put("docs/ui-guide.md", "# UI\n")
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- **Models**: UI/UX work → family:anthropic (project standard; follows [UI guide](docs/ui-guide.md)); "
            "SQL migrations → family:openai\n",
        )
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("family:anthropic", out)
        self.assertIn("family:openai", out)

        self.put("AGENTS.md", "## Orchestration\n- Models: UI/UX work → family:unknown-vendor\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("unknown family 'unknown-vendor'", out)
        self.assertIn("Models: ui → family:anthropic", out)

        self.put("AGENTS.md", "## Orchestration\n- Models: UI/UX work → family:anthropic trailing prose\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("unexpected trailing text", out)

    def test_high_risk_typed_parsing_and_source_existence(self):
        self.put("policy/risky-paths.txt", "auth/**\n")
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- `High risk`: db/migrations/**; src/[ab]/*.py; source:policy/risky-paths.txt; class:auth and payments\n",
        )
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("glob: db/migrations/**", out)
        self.assertIn("glob: src/[ab]/*.py", out)
        self.assertIn("source: policy/risky-paths.txt", out)
        self.assertIn("class: auth and payments", out)

        self.put("AGENTS.md", "## Orchestration\n- High risk: source:policy/missing.txt; /abs/path/**; bad***glob; empty[]glob\n")
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 1)
        self.assertIn("source file does not exist: policy/missing.txt", out)
        self.assertIn("glob must be repo-relative", out)
        self.assertIn("invalid glob '***'", out)
        self.assertIn("High risk: db/migrations/**; source:<path>; class:<phrase>", out)

    def test_brace_globs_are_not_treated_as_free_text(self):
        self.put("AGENTS.md", "## Orchestration\n- High risk: apps/{web,api}/**\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("brace expansion unsupported", out)
        self.assertIn("list each glob with ';'", out)
        self.assertNotIn("free-text format", out)

    def test_indented_continuations_and_non_bullet_errors(self):
        self.put(
            "AGENTS.md",
            "## Orchestration\n\n"
            "Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.\n\n"
            "- High risk: db/**;\n"
            "  auth/**; billing/**\n",
        )
        rc, out = self.run_policy()
        self.assertEqual(rc, 0, out)
        self.assertIn("glob: db/**", out)
        self.assertIn("glob: auth/**", out)
        self.assertIn("glob: billing/**", out)

        for bad in (
            "## Orchestration\n- High risk: db/**;\nauth/**\n",
            "## Orchestration\n+ Land: make land PR=<n>\n",
            "## Orchestration\n- High risk: db/**\n  - nested\n",
            "## Orchestration\n- Implement: x\nstray prose line between bullets\n- Land: `make land`\n",
        ):
            self.put("AGENTS.md", bad)
            rc, _ = self.run_policy()
            self.assertEqual(rc, 1, bad)

    def test_dual_location_warns_only_when_agents_md_has_bullets(self):
        self.put(".agents/orchestration.md", "# Orchestration\n- Land: `bun run land <pr>`\n")
        self.put("AGENTS.md", "# AGENTS\n\n## Orchestration\nSee [.agents/orchestration.md](.agents/orchestration.md).\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("warn:", out)

        self.put("AGENTS.md", "# AGENTS\n\n## Orchestration\n- Land: `make land`\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 0, out)
        self.assertIn("warn: both .agents/orchestration.md and AGENTS.md ## Orchestration exist", out)

    def test_default_repo_resolves_git_toplevel(self):
        _git(self.repo, "init", "-q", "-b", "main")
        self.put(".agents/orchestration.md", "- Known failures: /pod evicted/ => rerun\n")
        sub = os.path.join(self.repo, "packages", "core")
        os.makedirs(sub)
        prev = os.getcwd()
        try:
            os.chdir(sub)
            rc, out = _run_orch(orch.policy_cmd, _Args(repo=None, check=False, full=True, assets=self.assets, catalog=CATALOG_PATH))
            self.assertEqual(rc, 0, out)
            self.assertIn("policy: .agents/orchestration.md", out)
            rc, out = _run_orch(
                orch.failure_cmd,
                _Args(
                    message="pod evicted",
                    ref="PR-1",
                    known=None,
                    repo=None,
                    ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                    assets=self.assets,
                    now="2026-11-20T21:00:00Z",
                    full=True,
                ),
            )
            self.assertEqual(rc, 0, out)
            self.assertIn("known: pod evicted", out)
        finally:
            os.chdir(prev)

    def test_known_failures_typed_parsing_escapes_flags_and_arrow(self):
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Known failures: /forge (?:502|503)|runner lost/ => rerun; "
            "/lockfile;corrupt/ => `rm -rf node_modules && npm ci`; /tls\\/reset/ => rerun; /a => b/ => rerun\n",
        )
        rc, out = self.run_policy()
        self.assertEqual(rc, 0, out)
        self.assertIn("/forge (?:502|503)|runner lost/ => rerun", out)
        self.assertIn("/lockfile;corrupt/ => `rm -rf node_modules && npm ci`", out)
        self.assertIn("/tls\\/reset/ => rerun", out)
        self.assertIn("/a => b/ => rerun", out)

        self.put("AGENTS.md", "## Orchestration\n- Known failures: /(unclosed/ => rerun; /ok/ => restart it; /err/i => rerun\n")
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("invalid regex", out)
        self.assertIn("must be 'rerun' or a backticked command", out)
        self.assertIn("flags unsupported; matching is case-insensitive", out)
        self.assertIn("Known failures: /ETIMEDOUT/ => rerun", out)

    def test_source_realpath_must_stay_inside_repo(self):
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("secret\n")
        os.makedirs(os.path.join(self.repo, "policy"), exist_ok=True)
        os.symlink(outside, os.path.join(self.repo, "policy", "link.txt"))
        self.put("AGENTS.md", "## Orchestration\n- High risk: source:policy/link.txt\n")
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 1)
        self.assertIn("outside", out)

    def test_check_verifies_relative_links_and_skill_md(self):
        self.put(".agents/skills/build-plan/SKILL.md", "---\nname: build-plan\n---\n")
        self.put("docs/guide.md", "# Guide\n")
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Implement: [build-plan](.agents/skills/build-plan/SKILL.md) per [guide](docs/guide.md#guide).\n"
            "- Review: [missing](docs/missing.md) and [bad-skill](.agents/skills/nope/SKILL.md).\n",
        )
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 1)
        self.assertNotIn("build-plan", [l for l in out.splitlines() if l.startswith("error:")])
        self.assertIn("docs/missing.md", out)
        self.assertIn("SKILL.md", out)

    def test_free_text_format_rejected_and_action_does_not_tell_agent_to_edit(self):
        self.put("policy/risky.txt", "auth/**\n")
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Models: UI/UX work → Anthropic (team preference).\n"
            "- High risk: database schemas, CI workflows, auth and secrets (see [risk policy](policy/risky.txt)).\n"
            "- Known failures: registry 502 or lost runner → rerun once before investigating\n",
        )
        rc, out = self.run_policy()
        self.assertEqual(rc, 1)
        self.assertIn("Models: free-text format is not supported", out)
        self.assertIn("High risk: free-text format is not supported", out)
        self.assertIn("Known failures: free-text format is not supported", out)
        self.assertNotIn("compat", out)
        self.assertEqual(
            out.strip().splitlines()[-1],
            "error: 3 problem(s); action: proceed with skill defaults for the invalid field(s) and tell the user once (converting is their call; references/repo-policy.md)",
        )

        # A leftover compat flag in assets.yaml no longer enables free text
        self.write_assets("version: 1\ncompat:\n  legacy_orchestration_policy: true\n")
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 1)
        self.assertNotIn("warn: legacy", out)

        # failure_cmd ignores free-text Known failures and points at orch.py policy
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="registry 502",
                ref="PR-7",
                known=None,
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("note: repo Known failures has 1 invalid entry; run orch.py policy", out)
        self.assertNotIn("known: registry 502", out)

    def test_neutral_agents_section_layout(self):
        for sk in ("implement-feature", "code-review", "open-pr"):
            self.put(f".agents/skills/{sk}/SKILL.md", f"---\nname: {sk}\n---\n")
        self.put("docs/release.md", "# Release\n")
        self.put("docs/ui-guide.md", "# UI\n")
        self.put("policy/risky-paths.txt", "auth/**\n")
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Implement: [implement-feature](.agents/skills/implement-feature/SKILL.md).\n"
            "- Review: [code-review](.agents/skills/code-review/SKILL.md).\n"
            "- PR: [open-pr](.agents/skills/open-pr/SKILL.md).\n"
            "- Land: `make land PR=<n>` per [release guide](docs/release.md#land).\n"
            "- High risk: source:policy/risky-paths.txt; class:payments; class:auth\n"
            "- Models: UI/UX work → family:anthropic (follows [UI guide](docs/ui-guide.md))\n"
            "- Evidence: UI changes → screenshots; API changes → integration tests.\n"
            "- Checkpoints: new screens and public CLI flags.\n",
        )
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("ok: policy valid (8 fields)", out)

    def test_neutral_dot_agents_file_with_pointer_layout(self):
        for sk in ("implement-feature", "code-review", "open-pr"):
            self.put(f".agents/skills/{sk}/SKILL.md", f"---\nname: {sk}\n---\n")
        self.put("docs/release.md", "# Release\n")
        self.put("policy/risky-paths.txt", "deploy/**\n")
        self.put("AGENTS.md", "# AGENTS\n\nRepo policy lives in [.agents/orchestration.md](.agents/orchestration.md).\n")
        self.put(
            ".agents/orchestration.md",
            "# Orchestration\n\n"
            "- Implement: [implement-feature](.agents/skills/implement-feature/SKILL.md).\n"
            "- Review: [code-review](.agents/skills/code-review/SKILL.md).\n"
            "- PR: [open-pr](.agents/skills/open-pr/SKILL.md).\n"
            "- Land: `bun run land <pr>` per [release guide](docs/release.md).\n"
            "- High risk: source:policy/risky-paths.txt; deploy/**; config/**; class:payments; class:auth\n"
            "- Models: UI/UX work → family:anthropic; SQL migrations → family:openai\n"
            "- Known failures: /forge (?:502|503)|lost runner/ => rerun; /lockfile corrupt/ => `npm ci`\n",
        )
        rc, out = self.run_policy(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("policy: .agents/orchestration.md", out)
        self.assertIn("ok: policy valid (7 fields)", out)

    def test_failure_partial_known_failures_explicit_repo_note_and_non_utf8(self):
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Known failures: /pod evicted/ => rerun; /(broken/ => rerun\n",
        )
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="pod evicted",
                ref="PR-99",
                known=None,
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("note: repo Known failures has 1 invalid entry; run orch.py policy", out)
        self.assertIn("known: pod evicted", out)
        policy_rerun_action = out.strip().splitlines()[-1]

        # --known REGEX=>rerun agrees with policy rerun action
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="pod evicted",
                ref="PR-99",
                known=["pod evicted=>rerun"],
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], policy_rerun_action)

        # Explicit --repo with missing policy prints note; implicit missing policy stays silent
        empty_dir = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty_dir)
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="weird",
                ref="PR-99",
                known=None,
                repo=empty_dir,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("note: no repo policy found", out)

        # Non-UTF-8 AGENTS.md does not crash failure or policy
        with open(os.path.join(self.repo, "AGENTS.md"), "wb") as fh:
            fh.write(b"\xff\xfe## Orchestration\n- Known failures: /boom/ => rerun\n")
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="boom",
                ref="PR-99",
                known=None,
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        rc, _ = self.run_policy()
        self.assertEqual(rc, 0)

    def test_failure_autoloads_known_failures_from_repo_policy(self):
        self.put(
            "AGENTS.md",
            "## Orchestration\n"
            "- Known failures: /pod evicted/ => rerun; /stale cache/ => `rm -rf .cache && npm test`\n",
        )
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="build aborted: pod evicted during step 2",
                ref="PR-99",
                known=None,
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("class: infra", out)
        self.assertIn("known: pod evicted", out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("action: rerun"))

        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="stale cache checksum mismatch",
                ref="PR-99",
                known=None,
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "action: rm -rf .cache && npm test")

        # Explicit --known overrides repo policy
        rc, out = _run_orch(
            orch.failure_cmd,
            _Args(
                message="pod evicted",
                ref="PR-99",
                known=["pod evicted=>drain node"],
                repo=self.repo,
                ledger=os.path.join(self.tmp.name, "ledger.jsonl"),
                assets=self.assets,
                now="2026-11-20T21:00:00Z",
                full=True,
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "action: drain node")

    def test_check_rejects_removed_compat_key(self):
        self.write_assets("version: 1\ncompat:\n  legacy_orchestration_policy: true\n")
        rc, out = _run_orch(
            orch.check,
            _Args(assets=self.assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="", available=None, full=True),
        )
        self.assertEqual(rc, 1)
        self.assertIn("error: assets compat: removed in 0.14.0", out)
        self.assertIn("delete the key", out)


class TestSelectIdFormats(unittest.TestCase):
    """select accepts the ids discovery and its own output use, and rejects malformed --available before ranking."""

    AVAIL = "codex:gpt-6.2-sol,claudeAgent:claude-opus-5-5,claudeAgent:claude-sonnet-5-5,claudeAgent:claude-fable-5-2"

    def setUp(self):
        self.catalog = orch.load_file(CATALOG_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = os.path.join(self.tmp.name, "assets.yaml")
        with open(self.assets, "w", encoding="utf-8") as fh:
            fh.write("providers:\n  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-24T10:00:00Z\n")

    def select(self, available, implementer=None):
        args = _Args(complexity="standard", risk="medium", available=available, implementer=implementer, assets=self.assets,
                     now="2026-11-23T14:00:00Z", catalog=None, ui=False, prefer_family=None, fallback=None, below_tier=None,
                     ref=None, fail_on_low=False, full=False)
        return _run_orch(orch.select, args)

    def test_resolve_implementer_forms(self):
        for given in ("gpt-6.2-sol", "gpt-6-2-sol", "codex:gpt-6.2-sol", "codex/gpt-6-2-sol @ medium", "GPT-6.2 Sol",
                      "gpt-6-2-sol@medium", "codex:gpt-6.2-sol@xhigh"):
            m = orch.resolve_implementer(self.catalog, given)
            self.assertIsNotNone(m, given)
            self.assertEqual(m["id"], "gpt-6-2-sol", given)
        self.assertIsNone(orch.resolve_implementer(self.catalog, "no-such-model"))
        for given in ("codex_opencodex_api/openai-apikey/gpt-6-luna", "codex_opencodex_api:openai-apikey/gpt-6-luna"):
            self.assertEqual(orch.resolve_implementer(self.catalog, given)["id"], "gpt-6-luna", given)
        self.assertEqual(orch.resolve_implementer(self.catalog, "antigravity:Orchestrator (Neon) @ high")["id"], "gemini-pro-orchestrator-neon")

    def test_provider_prefixed_implementer_picks_non_emergency_reviewer(self):
        rc, out = self.select(self.AVAIL, implementer="codex:gpt-6.2-sol")
        self.assertEqual(rc, 0, out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: dispatch reviewer claudeAgent/claude-sonnet-5-5 @ high now")
        self.assertNotIn("fable", last)

    def test_bad_available_entries(self):
        good = ["codex:gpt-6.2-sol", "codex: gpt-6.2-sol", "claudeAgent_claude_extra:claude-sonnet-5-5",
                "codex_opencodex_api:openai-apikey/gpt-6-luna", "ollama:llama3:8b", "antigravity:Orchestrator (Neon)"]
        bad = ["claude-sonnet-5-5", "a:b c:d", ":x", "p:", "claudeAgent:claude-opus-5-5 claude-sonnet-5-5",
               "claudeAgent:claude-opus-5-5;codex:gpt-6.2-sol", "claudeAgent:claude-opus-5-5\ncodex:gpt-6.2-sol"]
        self.assertEqual(orch.bad_available_entries(good + bad, self.catalog), bad)

    def test_check_flags_the_same_entries_as_select(self):
        notes = orch.check_available_notes(self.catalog, self.assets, None, "claudeAgent:claude-sonnet-5-5 claudeAgent:claude-opus-5-5")
        self.assertIn("--available entry is not provider:model; ignored", [n[0] for n in notes])

    def test_newline_entry_stays_on_one_line(self):
        rc, out = self.select("claudeAgent:claude-opus-5-5\ncodex:gpt-6.2-sol", implementer="gpt-6.2-sol")
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(out.strip().splitlines()), 2, out)

    def test_malformed_available_errors_before_ranking(self):
        for avail in ("claude-sonnet-5-5,claudeAgent:claude-opus-5-5", "claudeAgent:claude-sonnet-5-5 claudeAgent:claude-opus-5-5"):
            rc, out = self.select(avail, implementer="gpt-6.2-sol")
            self.assertEqual(rc, 1, out)
            self.assertNotIn("research", out)
            self.assertTrue(out.strip().splitlines()[-1].startswith("action: fix --available"), out)


class TestClassifyAndOrchestratorRole(unittest.TestCase):
    """`orch.py classify`, `select --role orchestrator`, `preferences.orchestrator`, `coordinator`, and ledger extensions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = os.path.join(self.tmp.name, "assets.yaml")
        _write(
            self.assets,
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    policy: normal\n    status: ok\n"
            "  - id: claudeAgent\n    policy: normal\n    status: ok\n"
            "  - id: codex\n    policy: normal\n    status: ok\n",
        )

    def _classify(self, size, uncertainty, deps, consequences, verification, coordination, assets=None, full=False):
        return _run(
            orch.classify_cmd,
            _Args(
                size=size,
                uncertainty=uncertainty,
                deps=deps,
                consequences=consequences,
                verification=verification,
                coordination=coordination,
                assets=assets or self.assets,
                full=full,
            ),
        )

    def _orch_sel(self, available, coordination=2, assets=None, full=False, **kw):
        kw.setdefault("complexity", None)
        kw.setdefault("risk", None)
        return _run(
            orch.select,
            _sel(
                assets or self.assets,
                available,
                role="orchestrator",
                coordination=coordination,
                full=full,
                **kw,
            ),
        )

    def test_classify_exact_output_and_bands(self):
        rc, out = self._classify(2, 1, 2, 1, 1, 2)
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            out.splitlines(),
            [
                "complexity: hard (size 2 + uncertainty 1 + deps 2 + verification 1 = 6)",
                "risk: medium (consequences 1)",
                "orchestrator: T3 (coordination 2)",
                "rubric: size=2,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2",
                "action: select --complexity hard --risk medium for workers and select --role orchestrator --coordination 2 for the orchestrator; pass --rubric <rubric> to log",
            ],
        )
        bands = [
            ((0, 0, 0, 0, 0, 0), "complexity: trivial", "risk: low", "orchestrator: T1"),
            ((0, 1, 0, 0, 0, 0), "complexity: trivial", "risk: low", "orchestrator: T1"),
            ((1, 1, 0, 1, 0, 1), "complexity: standard", "risk: medium", "orchestrator: T2"),
            ((1, 1, 1, 1, 0, 1), "complexity: standard", "risk: medium", "orchestrator: T2"),
            ((1, 1, 1, 2, 1, 2), "complexity: hard", "risk: high", "orchestrator: T3"),
            ((2, 2, 2, 2, 1, 2), "complexity: frontier", "risk: high", "orchestrator: T3"),
            ((2, 2, 2, 2, 2, 2), "complexity: frontier", "risk: high", "orchestrator: T3"),
        ]
        for vals, c_exp, r_exp, o_exp in bands:
            rc2, out2 = self._classify(*vals)
            self.assertEqual(rc2, 0, out2)
            self.assertIn(c_exp, out2)
            self.assertIn(r_exp, out2)
            self.assertIn(o_exp, out2)

    def test_classify_custom_tiers_and_yaml_subset_nested_orchestrator(self):
        ap = os.path.join(self.tmp.name, "custom-orch.yaml")
        _write(
            ap,
            "version: 1\n"
            "preferences:\n"
            "  orchestrator:\n"
            "    tiers: {0: T2, 1: T3, 2: T4}\n"
            "    max_effort: high\n"
            "    defaults: {codex: medium, claude: high}\n"
            "    ladder: [codex:gpt-6-2-sol@medium, claude:claude-opus-5-5@high]\n"
            "    max_escalations: 2\n"
            "providers: []\n",
        )
        loaded = orch.load_file(ap)
        op = loaded["preferences"]["orchestrator"]
        self.assertEqual(op["tiers"], {"0": "T2", "1": "T3", "2": "T4"})
        self.assertEqual(op["defaults"], {"codex": "medium", "claude": "high"})
        self.assertEqual(op["ladder"], ["codex:gpt-6-2-sol@medium", "claude:claude-opus-5-5@high"])
        self.assertEqual(op["max_escalations"], 2)
        rc, out = self._classify(0, 0, 0, 0, 0, 0, assets=ap)
        self.assertEqual(rc, 0, out)
        self.assertIn("orchestrator: T2 (coordination 0)", out)

    def test_select_role_validation_errors(self):
        avail = "codex:gpt-6-2-sol"
        rc1, out1 = _run(orch.select, _sel(self.assets, avail, complexity=None, risk="low", full=False))
        self.assertEqual(rc1, 1, out1)
        self.assertTrue(out1.strip().splitlines()[-1].startswith("action: fix --complexity/--risk"), out1)

        rc2, out2 = self._orch_sel(avail, coordination=None)
        self.assertEqual(rc2, 1, out2)
        self.assertIn("--coordination", out2)

        rc3, out3 = self._orch_sel(avail, coordination=1, implementer="gpt-6-2-sol")
        self.assertEqual(rc3, 1, out3)
        self.assertIn("--implementer", out3)

    def test_select_orchestrator_ladder_coordinator_exclusion_and_provider_switch(self):
        avail = (
            "antigravity:gemini-4-argon,antigravity:gemini-4-neon,"
            "antigravity:gemini-pro-orchestrator-neon,antigravity:gemini-pro-orchestrator-g1-pro,"
            "claudeAgent:claude-opus-5-5"
        )
        rc, out = self._orch_sel(avail, coordination=2, full=False)
        self.assertEqual(rc, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "orchestrator tier T3 (>= 50; coordination 2)")
        self.assertEqual(
            lines[1],
            "ladder: antigravity/gemini-4-argon @ high -> antigravity/gemini-4-neon @ medium -> antigravity/gemini-4-neon @ high",
        )
        self.assertEqual(
            lines[2],
            "provider switch (phase boundary, ask the user first): claudeAgent/claude-opus-5-5 @ high",
        )
        self.assertIn("note: coordinator mode excluded: adds a second coordinator under yours:", out)
        self.assertIn("antigravity:gemini-pro-orchestrator-neon", out)
        self.assertIn("antigravity:gemini-pro-orchestrator-g1-pro", out)
        self.assertEqual(
            lines[-1],
            "action: launch the orchestrator on antigravity/gemini-4-argon @ high; "
            "climb the ladder only on an escalation trigger (references/selection.md); "
            "ask the user before the provider switch; log it with --role orchestrator --rubric <rubric>",
        )

    def test_select_orchestrator_defaults_max_effort_cap_and_max_escalations(self):
        ap = os.path.join(self.tmp.name, "prefs.yaml")
        _write(
            ap,
            "version: 1\n"
            "preferences:\n"
            "  orchestrator:\n"
            "    max_effort: xhigh\n"
            "    defaults: {codex: high}\n"
            "    max_escalations: 1\n"
            "providers:\n"
            "  - id: codex\n    policy: normal\n    status: ok\n",
        )
        rc, out = self._orch_sel("codex:gpt-6-2-sol", coordination=1, assets=ap, full=False)
        self.assertEqual(rc, 0, out)
        # defaults raises start rung from low (40.55) to high (54.89), and max_effort xhigh is capped to high with a note
        self.assertIn("ladder: codex/gpt-6-2-sol @ high", out)
        self.assertNotIn("@ xhigh", out)
        self.assertIn("max_effort", out)
        self.assertNotIn("provider switch", out)

        # max_escalations: 1 caps same-provider ladder to 2 rungs (1 + 1)
        rc2, out2 = self._orch_sel(
            "antigravity:gemini-4-argon,antigravity:gemini-4-neon", coordination=2, assets=ap, full=False
        )
        self.assertEqual(rc2, 0, out2)
        self.assertIn(
            "ladder: antigravity/gemini-4-argon @ high -> antigravity/gemini-4-neon @ medium",
            out2,
        )
        self.assertNotIn("gemini-4-neon @ high", out2)

    def test_select_orchestrator_explicit_ladder_filters_and_falls_back_to_auto(self):
        ap = os.path.join(self.tmp.name, "explicit.yaml")
        _write(
            ap,
            "version: 1\n"
            "preferences:\n"
            "  orchestrator:\n"
            "    ladder: [codex:gpt-6-2-sol@low, codex:gpt-6-2-sol@medium, codex:gpt-6-2-sol@high, antigravity:gemini-4-neon@high, missing:m@high]\n"
            "providers:\n"
            "  - id: codex\n    status: ok\n"
            "  - id: antigravity\n    status: ok\n",
        )
        # At coordination 2 (T3 >= 50), sol@low (40.55) is below floor, missing:m@high is not in --available
        rc, out = self._orch_sel("codex:gpt-6-2-sol,antigravity:gemini-4-neon", coordination=2, assets=ap, full=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("ladder: codex/gpt-6-2-sol @ medium -> codex/gpt-6-2-sol @ high", out)
        self.assertIn("provider switch (phase boundary, ask the user first): antigravity/gemini-4-neon @ high", out)
        self.assertIn("note:", out)

        # If no explicit ladder entries survive, fall back to auto with a note
        rc2, out2 = self._orch_sel("claudeAgent:claude-opus-5-5", coordination=2, assets=ap, full=False)
        self.assertEqual(rc2, 0, out2)
        self.assertIn("ladder: claudeAgent/claude-opus-5-5 @ high", out2)
        self.assertIn("auto", out2)

    def test_select_orchestrator_no_candidate_stops_without_staging(self):
        ap = os.path.join(self.tmp.name, "exh.yaml")
        _write(
            ap,
            "version: 1\n"
            "providers:\n"
            "  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: codex\n    status: ok\n",
        )
        # T3 (>=50): claude opus is exhausted, codex gpt-6-luna is below tier -> NO staging, exit 4 hold
        rc, out = self._orch_sel("claude:claude-opus-5-5,codex:gpt-6-luna", coordination=2, assets=ap, ref="orch-1", full=False)
        self.assertEqual(rc, 4, out)
        self.assertNotIn("stage:", out)
        self.assertIn("action: hold until 2026-11-23T08:02:00+00:00", out)
        self.assertIn("followup recorded: [resume] orch-1", out)

        # Gated (emergency only) -> exit 3
        rc3, out3 = self._orch_sel("codex:gpt-6-2-astra", coordination=2, assets=ap, full=False)
        self.assertEqual(rc3, 3, out3)

        # Only below-tier, no exhausted tier-meeting -> exit 2
        rc2, out2 = self._orch_sel("codex:gpt-6-luna", coordination=2, assets=ap, full=False)
        self.assertEqual(rc2, 2, out2)
        self.assertIn("action: no tier match:", out2)

    def test_local_catalog_carries_over_coordinator_flag(self):
        local_cat = os.path.join(self.tmp.name, "catalog.local.yaml")
        _write(
            local_cat,
            "models:\n"
            "  - id: gemini-pro-orchestrator-neon\n"
            "    name: Orchestrator (Neon)\n"
            "    family: google\n"
            "    cost: cheap\n"
            "    rating: estimated\n"
            "    scores: {high: 62}\n"
            "  - id: gemini-pro-orchestrator-g1-pro\n"
            "    name: Orchestrator (Argon)\n"
            "    family: google\n"
            "    cost: cheap\n"
            "    rating: estimated\n"
            "    coordinator: false\n"
            "    scores: {high: 52}\n",
        )
        merged = orch.load_catalog_with_local(CATALOG_PATH, self.assets)
        by_id = {m["id"]: m for m in merged["models"]}
        self.assertTrue(by_id["gemini-pro-orchestrator-neon"].get("coordinator"))
        self.assertFalse(by_id["gemini-pro-orchestrator-g1-pro"].get("coordinator"))

        rc, out = self._orch_sel(
            "antigravity:gemini-pro-orchestrator-neon,antigravity:gemini-pro-orchestrator-g1-pro",
            coordination=2,
            full=False,
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("ladder: antigravity/gemini-pro-orchestrator-g1-pro @ high", out)
        self.assertIn("coordinator mode excluded: adds a second coordinator under yours", out)

    def test_check_validates_orchestrator_prefs_and_catalog_coordinator(self):
        good = os.path.join(self.tmp.name, "good-orch.yaml")
        _write(
            good,
            "version: 1\n"
            "preferences:\n"
            "  orchestrator:\n"
            "    tiers: {0: T1, 1: T2, 2: T3}\n"
            "    max_effort: high\n"
            "    defaults: {codex: medium, claude: high}\n"
            "    ladder: auto\n"
            "    max_escalations: 2\n"
            "providers: []\n",
        )
        rc, out = _run(orch.check, _Args(assets=good, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
        self.assertEqual(rc, 0, out)

        bad = os.path.join(self.tmp.name, "bad-orch.yaml")
        _write(
            bad,
            "version: 1\n"
            "preferences:\n"
            "  orchestrator:\n"
            "    tiers: {3: T9}\n"
            "    max_effort: xhigh\n"
            "    defaults: {codex: xhigh}\n"
            "    ladder: [bad-entry]\n"
            "    max_escalations: 5\n"
            "providers: []\n",
        )
        rc2, out2 = _run(orch.check, _Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
        self.assertEqual(rc2, 1, out2)
        for token in ("tiers", "max_effort", "defaults", "ladder", "max_escalations"):
            self.assertIn(token, out2)

        bad_cat = os.path.join(self.tmp.name, "bad-cat.yaml")
        _write(
            bad_cat,
            "version: 1\nmodels:\n  - id: m1\n    name: M1\n    family: f\n    cost: normal\n    rating: unrated\n    coordinator: nope\n",
        )
        rc3, out3 = _run(orch.check, _Args(assets=good, catalog=bad_cat, now="2026-11-20T21:00:00Z", lockfile=""))
        self.assertEqual(rc3, 1, out3)
        self.assertIn("coordinator", out3)

    def test_log_role_rubric_requested_effective_mismatch_and_history(self):
        ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        base_log = dict(
            assets=self.assets,
            ledger=ledger,
            now="2026-11-20T21:00:00Z",
            event="select",
            complexity="hard",
            risk="medium",
            ui=False,
            surface="conversation",
            provider="antigravity",
            model="gemini-4-neon",
            effort="high",
            reviewer=None,
            outcome=None,
            findings=None,
            repo="demo",
            provisional=False,
            ref="ORCH-1",
            failure_class=None,
        )
        rc, out = _run(
            orch.log_cmd,
            _Args(
                **base_log,
                role="orchestrator",
                rubric="size=2,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2",
                requested="antigravity:gemini-4-argon@high",
                effective="antigravity:gemini-4-neon@high",
            ),
        )
        self.assertEqual(rc, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(
            lines[0],
            "warning: effective antigravity:gemini-4-neon@high differs from requested antigravity:gemini-4-argon@high; check the thread settings",
        )
        self.assertTrue(lines[1].startswith("logged select"), out)

        with open(ledger, encoding="utf-8") as fh:
            row = json.loads(fh.read().strip())
        self.assertEqual(row["role"], "orchestrator")
        self.assertEqual(
            row["rubric"],
            {"size": 2, "uncertainty": 1, "deps": 2, "consequences": 1, "verification": 1, "coordination": 2},
        )
        self.assertEqual(row["requested"], "antigravity:gemini-4-argon@high")
        self.assertEqual(row["effective"], "antigravity:gemini-4-neon@high")
        self.assertTrue(row["mismatch"])

        rc_h, out_h = _run(orch.history_cmd, _hist(ledger, assets=self.assets))
        self.assertEqual(rc_h, 0, out_h)
        self.assertIn("role orchestrator", out_h)
        self.assertIn("effective differs", out_h)

        # Implementer role with matching requested/effective omits role label and mismatch
        rc_ok, out_ok = _run(
            orch.log_cmd,
            _Args(
                **{**base_log, "ref": "IMPL-1"},
                role="implementer",
                rubric=None,
                requested="antigravity:gemini-4-neon@high",
                effective="antigravity:gemini-4-neon@high",
            ),
        )
        self.assertEqual(rc_ok, 0, out_ok)
        self.assertNotIn("warning:", out_ok)
        rc_h2, out_h2 = _run(orch.history_cmd, _hist(ledger, assets=self.assets, ref="IMPL-1"))
        self.assertEqual(rc_h2, 0, out_h2)
        self.assertNotIn("role implementer", out_h2)
        self.assertNotIn("effective differs", out_h2)

        # Validation failures
        for bad_kw in (
            {"rubric": "size=2,uncertainty=1"},
            {"rubric": "size=3,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2"},
            {"requested": "no-provider@high"},
            {"effective": "codex:gpt-6-2-sol"},
        ):
            rc_b, _ = _run(orch.log_cmd, _Args(**{**base_log, "role": None, "rubric": None, "requested": None, "effective": None, **bad_kw}))
            self.assertEqual(rc_b, 1, bad_kw)

    def test_non_string_tier_value_does_not_crash(self):
        ap = os.path.join(self.tmp.name, "list-tier.yaml")
        _write(ap, "version: 1\npreferences:\n  orchestrator:\n    tiers: {2: [T3]}\nproviders:\n  - id: codex\n    status: ok\n")
        rc_c, out_c = self._classify(2, 1, 2, 1, 1, 2, assets=ap)
        self.assertEqual(rc_c, 0, out_c)
        self.assertIn("orchestrator: T3 (coordination 2)", out_c)
        rc_s, out_s = self._orch_sel("codex:gpt-6-2-sol", coordination=2, assets=ap)
        self.assertEqual(rc_s, 0, out_s)
        rc_k, out_k = _run(orch.check, _Args(assets=ap, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
        self.assertEqual(rc_k, 1, out_k)
        self.assertIn("tiers[2]", out_k)

    def test_provider_switch_picks_escalation_or_notes_non_escalation(self):
        # Branch 2 (no other provider exceeds ladder top 54.89): picks opus@high (51.84), not sonnet@high (41.52), and notes non-escalation
        ap = os.path.join(self.tmp.name, "sw.yaml")
        _write(ap, "version: 1\nproviders:\n  - id: codex\n    policy: spend-first\n    status: ok\n  - id: claude\n    policy: normal\n    status: ok\n  - id: antigravity\n    policy: normal\n    status: ok\n")
        rc1, out1 = self._orch_sel("codex:gpt-6-2-sol,claude:claude-sonnet-5-5,claude:claude-opus-5-5", coordination=1, assets=ap)
        self.assertEqual(rc1, 0, out1)
        self.assertIn("provider switch (phase boundary, ask the user first): claude/claude-opus-5-5 @ high", out1)
        self.assertIn("provider switch is not an escalation (score 51.84 <= ladder top 54.89)", out1)

        # Branch 1 (other provider exceeds ladder top 54.89): picks lowest such effort (gemini-4-neon @ medium = 55), no non-escalation note
        rc2, out2 = self._orch_sel("codex:gpt-6-2-sol,antigravity:gemini-4-argon,antigravity:gemini-4-neon", coordination=1, assets=ap)
        self.assertEqual(rc2, 0, out2)
        self.assertIn("provider switch (phase boundary, ask the user first): antigravity/gemini-4-neon @ medium", out2)
        self.assertNotIn("not an escalation", out2)

    def test_explicit_ladder_respects_max_escalations(self):
        ap = os.path.join(self.tmp.name, "exp-cap.yaml")
        _write(
            ap,
            "version: 1\npreferences:\n  orchestrator:\n    max_escalations: 1\n"
            "    ladder: [codex:gpt-6-2-sol@low, codex:gpt-6-2-sol@medium, codex:gpt-6-2-sol@high]\n"
            "providers:\n  - id: codex\n    status: ok\n",
        )
        rc, out = self._orch_sel("codex:gpt-6-2-sol", coordination=1, assets=ap)
        self.assertEqual(rc, 0, out)
        self.assertIn("ladder: codex/gpt-6-2-sol @ low -> codex/gpt-6-2-sol @ medium\n", out)
        self.assertNotIn("@ high", out)

    def test_orchestrator_hold_records_provider_scoped_needs_not_spurious_due(self):
        ap = os.path.join(self.tmp.name, "hold-t4.yaml")
        _write(
            ap,
            "version: 1\npreferences:\n  orchestrator:\n    tiers: {2: T4}\nproviders:\n"
            "  - id: antigravity\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: claude\n    status: ok\n",
        )
        avail = "antigravity:gemini-4-neon,claude:claude-opus-5-5"
        rc, out = self._orch_sel(avail, coordination=2, assets=ap, ref="orch-t4")
        self.assertEqual(rc, 4, out)
        self.assertIn("needs provider:antigravity/gemini-4-neon", out)
        rc_f, out_f = _run(orch.followups_cmd, _fu(ap, due=True, available=avail, full=False))
        self.assertEqual(rc_f, 0, out_f)
        _assert_none_due(self, out_f)

    def test_log_requested_effective_normalization_and_select_format(self):
        ledger = os.path.join(self.tmp.name, "norm-ledger.jsonl")
        rc, out = _run(
            orch.log_cmd,
            _Args(
                assets=self.assets, ledger=ledger, now="2026-11-20T21:00:00Z", event="select",
                complexity="hard", risk="low", ui=False, surface="conversation",
                provider="claudeAgent", model="claude-opus-5-5", effort="high",
                reviewer=None, outcome=None, findings=None, repo="demo", provisional=False,
                ref="NORM-1", failure_class=None, role=None, rubric=None,
                requested="claudeAgent/claude-opus-5-5 @ high",
                effective="ClaudeAgent:Claude Opus 5.5@HIGH",
            ),
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("warning:", out)
        with open(ledger, encoding="utf-8") as fh:
            row = json.loads(fh.read().strip())
        self.assertEqual(row["role"], "implementer")
        self.assertEqual(row["requested"], "claudeAgent:claude-opus-5-5@high")  # the provider id stays verbatim
        self.assertEqual(row["effective"], "ClaudeAgent:claude-opus-5-5@high")
        self.assertNotIn("mismatch", row)  # equal up to provider case

    def test_select_warns_on_ignored_other_role_flags_and_equal_score_rung(self):
        rc1, out1 = self._orch_sel("codex:gpt-6-2-sol", coordination=1, complexity="hard", ui=True, below_tier="wait")
        self.assertEqual(rc1, 0, out1)
        self.assertIn("note: ignored for --role orchestrator: --complexity, --ui, --below-tier", out1)

        rc2, out2 = _run(orch.select, _sel(self.assets, "codex:gpt-6-2-sol", complexity="standard", risk="low", coordination=2, full=False))
        self.assertEqual(rc2, 0, out2)
        self.assertIn("note: ignored for --role implementer: --coordination", out2)

        rc3, out3 = _run(orch.select, _sel(self.assets, "codex:gpt-6-2-sol,claudeAgent:claude-opus-5-5", complexity="standard", risk="low", role="reviewer", implementer="gpt-6-2-sol", coordination=1, full=False))
        self.assertEqual(rc3, 0, out3)
        self.assertIn("note: ignored for --role reviewer: --coordination", out3)

        # A10: equal-score higher effort on start model must not consume a rung
        flat_cat = os.path.join(self.tmp.name, "flat-cat.yaml")
        _write(flat_cat, "version: 1\nindex_versions:\n  v1: {name: v1}\nmodels:\n  - id: flat-m\n    name: Flat\n    family: f\n    cost: normal\n    rating: rated\n    source: v1\n    scores: {medium: 52, high: 52}\n")
        rc4, out4 = self._orch_sel("codex:flat-m", coordination=2, catalog=flat_cat)
        self.assertEqual(rc4, 0, out4)
        self.assertIn("ladder: codex/flat-m @ medium\n", out4)
        self.assertNotIn("@ high", out4)

    def test_role_annex_select_history_and_log_help(self):
        avail = "codex:gpt-6-2-sol,claudeAgent:claude-opus-5-5"
        rc_m, out_m = _run(orch.select, _sel(self.assets, avail, complexity="standard", risk="low", role="reviewer", implementer=None, full=False))
        self.assertEqual(rc_m, 1, out_m)
        self.assertIn("--implementer", out_m)
        rc_b, out_b = _run(orch.select, _sel(self.assets, avail, complexity="standard", risk="low", role="implementer", implementer="gpt-6-2-sol", full=False))
        self.assertEqual(rc_b, 1, out_b)

        ledger = os.path.join(self.tmp.name, "roles.jsonl")
        _write(ledger, "\n".join(json.dumps(e) for e in (
            {"event": "select", "ref": "OLD-1", "provider": "codex", "model": "gpt-6-2-sol", "effort": "medium"},
            {"event": "select", "ref": "ORCH-1", "provider": "antigravity", "model": "gemini-4-neon", "effort": "high", "role": "orchestrator"},
            {"event": "select", "ref": "REV-1", "provider": "claude", "model": "claude-opus-5-5", "effort": "high", "role": "reviewer"},
        )) + "\n")
        rc_o, out_o = _run(orch.history_cmd, _Args(ledger=ledger, assets=self.assets, ref=None, last=None, role="orchestrator", full=False))
        self.assertEqual(rc_o, 0, out_o)
        self.assertIn("ORCH-1:", out_o)
        self.assertNotIn("OLD-1:", out_o)
        rc_i, out_i = _run(orch.history_cmd, _Args(ledger=ledger, assets=self.assets, ref=None, last=None, role="implementer", full=False))
        self.assertEqual(rc_i, 0, out_i)
        self.assertIn("OLD-1:", out_i)
        self.assertNotIn("ORCH-1:", out_i)

        help_out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS_DIR, "orch.py"), "log", "--help"], stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertLessEqual(len(help_out.splitlines()), 22, help_out)
        self.assertIn("K=N,...", help_out)
        self.assertIn("P:M@E", help_out)


class TestDeferGateAndExhaustNext(unittest.TestCase):
    """Audit patterns B, C, D and legitimate holds: orch.py defer, exhaust with selection args, and exhaust --self."""

    NOW = "2026-11-22T02:00:00Z"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_assets(self, text, name="assets.yaml"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _defer(self, assets, **kw):
        defaults = dict(
            ref="item-1",
            role="implementer",
            complexity="standard",
            risk="medium",
            coordination=None,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            implementer=None,
            ui=False,
            prefer_family=None,
            below_tier=None,
            user_hold=False,
            needs=None,
            after=None,
            on_recovery=False,
            pool_busy=None,
            pool_cap=2,
            catalog=CATALOG_PATH,
            assets=assets,
            now=self.NOW,
            full=False,
        )
        defaults.update(kw)
        return _run_orch(orch.defer_cmd, _Args(**defaults))

    def _exhaust(self, assets, provider, message, **kw):
        defaults = dict(
            provider=provider,
            message=message,
            group=None,
            model=None,
            is_self=False,
            complexity=None,
            risk=None,
            coordination=None,
            available=None,
            role=None,
            implementer=None,
            ui=False,
            below_tier=None,
            ref=None,
            catalog=CATALOG_PATH,
            assets=assets,
            now=self.NOW,
            full=False,
        )
        defaults.update(kw)
        return _run_orch(orch.exhaust, _Args(**defaults))

    def test_pattern_c_defer_refuses_when_dispatch_available_including_low_preserve_pool(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    policy: preserve\n    status: low\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
        )
        rc, out = self._defer(
            ap,
            ref="PR-101",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 5, out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: dispatch claudeAgent/claude-opus-5-5 @ xhigh now; do not defer PR-101")

    def test_pattern_c_defer_refuses_when_stage_available_and_records_resume(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n"
        )
        rc, out = self._defer(
            ap,
            ref="PR-stage",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 5, out)
        self.assertIn("followup recorded: [resume] PR-stage", out)
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("action: stage: dispatch Phase 1 codex/gpt-6-2-sol @ xhigh now;"), out)
        self.assertTrue(last.endswith("; do not defer PR-stage"), out)
        fus = (orch.load_file(ap) or {}).get("followups") or []
        self.assertEqual(len(fus), 1)
        self.assertEqual(fus[0]["kind"], "resume")
        self.assertEqual(fus[0]["ref"], "PR-stage")
        self.assertEqual(fus[0]["after"], "2026-11-22T06:52:00+00:00")

    def test_pattern_b_defer_reviewer_dispatches_same_family_with_degradation_note(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
        )
        rc, out = self._defer(
            ap,
            ref="PR-rev",
            role="reviewer",
            complexity="standard",
            risk="medium",
            available="claudeAgent:claude-opus-5-5,claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
            implementer="claudeAgent:claude-opus-5-5",
        )
        self.assertEqual(rc, 5, out)
        self.assertIn("[note] reviewer degraded:", out)
        last = out.strip().splitlines()[-1]
        self.assertIn("claudeAgent/claude-sonnet-5-5 @ high now", last)
        self.assertIn("do not defer PR-rev", last)

    def test_correct_hold_when_all_tier_providers_exhausted_passes_through_and_records_followup(self):
        ap = self._write_assets(
            "version: 1\npreferences:\n  below_tier: wait\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:49:20+00:00\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T08:00:00+00:00\n"
            "  - id: antigravity\n    status: exhausted\n    exhausted_until: 2026-11-22T09:00:00+00:00\n"
            "  - id: local-small\n    status: ok\n"
        )
        rc, out = self._defer(
            ap,
            ref="PR-hold",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol,antigravity:gemini-4-neon,local-small:claude-haiku-5-5",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("followup recorded: [resume] PR-hold", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: hold until 2026-11-22T06:51:20+00:00; follow-up recorded: schedule its wakeup")
        saved = orch.load_file(ap)
        fus = saved.get("followups") or []
        self.assertEqual(len(fus), 1)
        self.assertEqual(fus[0]["kind"], "resume")
        self.assertEqual(fus[0]["ref"], "PR-hold")
        self.assertEqual(fus[0]["after"], "2026-11-22T06:51:20+00:00")

    def test_defer_pool_at_cap_queues_only_when_no_free_pool_and_supports_grouped_pools(self):
        # All three pools ok, only codex at cap -> drops codex and dispatches a free pool instead of queueing!
        ap_multi = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n"
            "  - id: antigravity\n    status: ok\n",
            "multi.yaml",
        )
        rc_free, out_free = self._defer(
            ap_multi,
            ref="PR-free",
            complexity="standard",
            risk="medium",
            available="codex:gpt-6-2-sol,claudeAgent:claude-sonnet-5-5,antigravity:gemini-4-neon",
            pool_busy="codex=2",
            pool_cap=2,
        )
        self.assertEqual(rc_free, 5, out_free)
        last_free = out_free.strip().splitlines()[-1]
        self.assertNotIn("codex/", last_free)
        self.assertIn("do not defer PR-free", last_free)

        # Only claudeAgent usable and capped -> queues for next batch without quota follow-up.
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
        )
        rc, out = self._defer(
            ap,
            ref="PR-batch2",
            complexity="standard",
            risk="medium",
            available="claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="claudeAgent=2",
            pool_cap=2,
        )
        self.assertEqual(rc, 0, out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(
            last,
            "action: queue PR-batch2 for the next batch (pool claudeAgent at cap 2/2); treat as a batch cap, not a quota wait",
        )
        self.assertFalse((orch.load_file(ap) or {}).get("followups"))

        # Below cap still refuses deferral.
        rc_low, out_low = self._defer(
            ap,
            ref="PR-batch2",
            complexity="standard",
            risk="medium",
            available="claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="claudeAgent=1",
            pool_cap=2,
        )
        self.assertEqual(rc_low, 5, out_low)
        self.assertIn("do not defer PR-batch2", out_low.strip().splitlines()[-1])

        # Grouped provider: Gemini Models capped, Gemini 4 Argon free -> dispatches Argon; when Argon exhausted -> queues naming provider/group.
        ap_grp = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    status: ok\n    groups:\n"
            "      - name: Gemini Models\n        models: [gemini-4-neon]\n        status: ok\n        window: 7d\n"
            "      - name: Gemini 4 Argon\n        models: [gemini-4-argon]\n        status: ok\n        window: 7d\n",
            "grp.yaml",
        )
        rc_g1, out_g1 = self._defer(
            ap_grp,
            ref="PR-grp",
            complexity="hard",
            risk="low",
            available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
            pool_busy="antigravity/Gemini Models=2",
            pool_cap=2,
        )
        self.assertEqual(rc_g1, 5, out_g1)
        self.assertIn("antigravity/gemini-4-argon @ high now; do not defer PR-grp", out_g1.strip().splitlines()[-1])

        rc_g2, out_g2 = self._defer(
            ap_grp,
            ref="PR-grp",
            complexity="hard",
            risk="low",
            available="antigravity:gemini-4-neon,antigravity:gemini-4-argon",
            pool_busy="antigravity/Gemini Models=2,antigravity/Gemini 4 Argon=2",
            pool_cap=2,
        )
        self.assertEqual(rc_g2, 0, out_g2)
        self.assertIn("pool antigravity/Gemini 4 Argon at cap 2/2", out_g2.strip().splitlines()[-1])

    def test_defer_pool_cap_with_hold_and_with_stage(self):
        # All exhausted + pool-busy -> still holds and records follow-up.
        ap_hold = self._write_assets(
            "version: 1\npreferences:\n  below_tier: wait\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T08:00:00+00:00\n",
            "cap-hold.yaml",
        )
        rc_h, out_h = self._defer(
            ap_hold,
            ref="PR-cap-hold",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            pool_busy="codex=2",
            pool_cap=2,
        )
        self.assertEqual(rc_h, 0, out_h)
        self.assertIn("followup recorded: [resume] PR-cap-hold", out_h)
        self.assertIn("action: hold until 2026-11-22T06:52:00+00:00", out_h.strip().splitlines()[-1])

        # Stage plan with only claudeAgent=2 busy (Phase 1 pool codex is NOT capped) -> refuses and dispatches Phase 1 on codex!
        ap_stage = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "cap-stage.yaml",
        )
        rc_s_free, out_s_free = self._defer(
            ap_stage,
            ref="PR-cap-stage-free",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            pool_busy="claudeAgent=2",
            pool_cap=2,
        )
        self.assertEqual(rc_s_free, 5, out_s_free)
        self.assertIn("stage: dispatch Phase 1 codex/gpt-6-2-sol @ xhigh now", out_s_free.strip().splitlines()[-1])
        self.assertIn("do not defer PR-cap-stage-free", out_s_free.strip().splitlines()[-1])

        # Stage candidate pool is capped and no free pool exists -> queues without writing a stage follow-up.
        ap_stage_cap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "cap-stage-2.yaml",
        )
        rc_s, out_s = self._defer(
            ap_stage_cap,
            ref="PR-cap-stage",
            complexity="hard",
            risk="high",
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            pool_busy="codex=2",
            pool_cap=2,
        )
        self.assertEqual(rc_s, 0, out_s)
        self.assertEqual(
            out_s.strip().splitlines()[-1],
            "action: queue PR-cap-stage for the next batch (pool codex at cap 2/2); treat as a batch cap, not a quota wait",
        )
        self.assertFalse((orch.load_file(ap_stage_cap) or {}).get("followups"))

    def test_defer_pool_cap_preserves_ui_prefer_family_reviewer_and_provider_label(self):
        # N1: --ui with ui_fallback: ask and claude capped (codex free) -> queues on claude, never skips ask gate.
        ap_ui_ask = self._write_assets(
            "version: 1\npreferences:\n  ui_fallback: ask\nproviders:\n"
            "  - id: claude\n    status: ok\n"
            "  - id: codex\n    status: ok\n",
            "cap-ui-ask.yaml",
        )
        rc, out = self._defer(
            ap_ui_ask,
            ref="PR-ui-cap",
            complexity="standard",
            risk="low",
            ui=True,
            available="claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="claude=2",
            pool_cap=2,
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("not offered", out)
        self.assertIn("pool claude at cap 2/2", out.strip().splitlines()[-1])

        # N1: --ui under ui_fallback: proceed with claude capped -> also queues on claude (no silent non-provisional dispatch).
        ap_ui_proc = self._write_assets(
            "version: 1\npreferences:\n  ui_fallback: proceed\nproviders:\n"
            "  - id: claude\n    status: ok\n"
            "  - id: codex\n    status: ok\n",
            "cap-ui-proc.yaml",
        )
        rc_p, out_p = self._defer(
            ap_ui_proc,
            ref="PR-ui-proc",
            complexity="standard",
            risk="low",
            ui=True,
            available="claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="claude=2",
            pool_cap=2,
        )
        self.assertEqual(rc_p, 0, out_p)
        self.assertIn("pool claude at cap 2/2", out_p.strip().splitlines()[-1])

        # N1: --prefer-family anthropic with claude capped -> queues on claude instead of dispatching codex.
        rc_f, out_f = self._defer(
            ap_ui_proc,
            ref="PR-fam-cap",
            complexity="standard",
            risk="low",
            prefer_family="anthropic",
            available="claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="claude=2",
            pool_cap=2,
        )
        self.assertEqual(rc_f, 0, out_f)
        self.assertIn("pool claude at cap 2/2", out_f.strip().splitlines()[-1])

        # N1: reviewer with implementer codex and claude capped -> queues on claude, never picks implementer's own model!
        rc_r, out_r = self._defer(
            ap_ui_proc,
            ref="PR-rev-cap",
            role="reviewer",
            complexity="standard",
            risk="medium",
            available="claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
            implementer="codex:gpt-6-2-sol",
            pool_busy="claude=2",
            pool_cap=2,
        )
        self.assertEqual(rc_r, 0, out_r)
        self.assertIn("pool claude at cap 2/2", out_r.strip().splitlines()[-1])

        # N6: provider-level cap on a grouped provider must be labelled as provider only (no group suffix).
        ap_grp = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    status: ok\n    groups:\n"
            "      - name: Gemini Models\n        models: [gemini-4-neon]\n        status: ok\n        window: 7d\n",
            "cap-prov-grp.yaml",
        )
        rc_pg, out_pg = self._defer(
            ap_grp,
            ref="PR-prov-cap",
            complexity="hard",
            risk="low",
            available="antigravity:gemini-4-neon",
            pool_busy="antigravity=2",
            pool_cap=2,
        )
        self.assertEqual(rc_pg, 0, out_pg)
        self.assertIn("(pool antigravity at cap 2/2)", out_pg.strip().splitlines()[-1])
        self.assertNotIn("Gemini Models", out_pg.strip().splitlines()[-1])

    def test_defer_user_hold_validation_recording_and_repeat_in_place(self):
        ap = self._write_assets("version: 1\nproviders:\n  - id: codex\n    status: ok\n")
        # Missing --needs -> error rc=1.
        rc_no_needs, out_no_needs = self._defer(
            ap,
            ref="batch-user",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs=None,
        )
        self.assertEqual(rc_no_needs, 1, out_no_needs)
        self.assertIn("--needs", out_no_needs.strip().splitlines()[-1])

        # Bad --after -> error rc=1.
        rc_bad_after, out_bad_after = self._defer(
            ap,
            ref="batch-user",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:codex/T3",
            after="not-an-iso-timestamp",
        )
        self.assertEqual(rc_bad_after, 1, out_bad_after)
        self.assertIn("--after", out_bad_after.strip().splitlines()[-1])

        # Valid user hold records resume follow-up.
        rc, out = self._defer(
            ap,
            ref="batch-user",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:codex/T3",
            after="2026-11-23T08:02:00+00:00",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("followup recorded: [resume] batch-user", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: hold batch-user as the user asked; follow-up recorded: schedule its wakeup")
        fus = orch.load_file(ap).get("followups") or []
        self.assertEqual(len(fus), 1)
        self.assertEqual(fus[0]["kind"], "resume")
        self.assertEqual(fus[0]["ref"], "batch-user")
        self.assertEqual(fus[0]["needs"], "provider:codex/T3")
        self.assertEqual(fus[0]["after"], "2026-11-23T08:02:00+00:00")

        # Repeated identical user hold says follow-up in place.
        rc2, out2 = self._defer(
            ap,
            ref="batch-user",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:codex/T3",
            after="2026-11-23T08:02:00+00:00",
        )
        self.assertEqual(rc2, 0, out2)
        self.assertIn("followup already scheduled: [resume] batch-user", out2)
        self.assertEqual(
            out2.strip().splitlines()[-1],
            "action: hold batch-user as the user asked; follow-up in place: schedule its wakeup",
        )

    def test_defer_user_hold_canonicalizes_needs_and_warns_on_unlisted_provider(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    status: ok\n    groups:\n"
            '      - name: "Gemini Models"\n        models: [gemini-4-neon, gemini-4-hydrogen]\n        status: exhausted\n'
            "        exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "canon-needs.yaml",
        )
        # Group shorthand + model display-name alias rewrite to canonical group name and model id so _model_matches_need matches.
        rc_g, out_g = self._defer(
            ap,
            ref="user-grp",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:antigravity/Gemini/T4",
            after="2026-11-23T08:02:00+00:00",
        )
        self.assertEqual(rc_g, 0, out_g)
        rc_m, out_m = self._defer(
            ap,
            ref="user-alias",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="GPT-6.2 Sol",
            after="2026-11-23T08:02:00+00:00",
        )
        self.assertEqual(rc_m, 0, out_m)
        by_ref = {fu["ref"]: fu for fu in (orch.load_file(ap).get("followups") or [])}
        self.assertEqual(by_ref["user-grp"]["needs"], "provider:antigravity/Gemini Models/T4")
        self.assertEqual(by_ref["user-alias"]["needs"], "gpt-6-2-sol")

        # Verify find_usable_in_available actually matches both stored canonical needs once renewed.
        cat = orch.load_file(CATALOG_PATH)
        loaded = orch.load_file(ap)
        after_reset = orch.get_now("2026-11-23T08:05:00Z")
        self.assertTrue(
            orch.find_usable_in_available(cat, loaded, after_reset, by_ref["user-grp"]["needs"], ["antigravity:gemini-4-neon"])
        )
        self.assertTrue(
            orch.find_usable_in_available(cat, loaded, after_reset, by_ref["user-alias"]["needs"], ["codex:gpt-6-2-sol"])
        )

        # Model id used as a group name must be rejected rather than stored as a group.
        rc_bad_grp, out_bad_grp = self._defer(
            ap,
            ref="user-bad-grp",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:antigravity/gemini-4-neon/T4",
        )
        self.assertEqual(rc_bad_grp, 1, out_bad_grp)
        self.assertIn("unknown quota group", out_bad_grp)

        # Unlisted provider is allowed with a note (unlisted providers default to ok).
        rc_unlisted, out_unlisted = self._defer(
            ap,
            ref="user-unlisted",
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:new-prov/T3",
            after="2026-11-23T08:02:00+00:00",
        )
        self.assertEqual(rc_unlisted, 0, out_unlisted)
        self.assertIn("note: provider new-prov is not in assets.yaml (unlisted providers default to ok)", out_unlisted)
        self.assertIn("followup recorded: [resume] user-unlisted", out_unlisted)

    def test_defer_role_orchestrator_ladder_cap_hold_and_validation(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n",
            "orch-defer.yaml",
        )
        # Usable orchestrator exists -> refuses deferral with rc=5 and names launch target.
        rc, out = self._defer(
            ap,
            ref="orch-1",
            role="orchestrator",
            complexity=None,
            risk=None,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 5, out)
        self.assertIn("orchestrator tier T3", out)
        self.assertIn("ladder:", out)
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("action: launch the orchestrator on "), out)
        self.assertIn("; do not defer orch-1;", last)
        self.assertTrue(last.endswith("; log it with --role orchestrator --rubric <rubric>"), out)

        # Only usable orchestrator pool is at cap -> queues for next batch without quota follow-up.
        ap_cap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "orch-cap.yaml",
        )
        rc_cap, out_cap = self._defer(
            ap_cap,
            ref="orch-cap",
            role="orchestrator",
            complexity=None,
            risk=None,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            pool_busy="codex=2",
            pool_cap=2,
        )
        self.assertEqual(rc_cap, 0, out_cap)
        self.assertEqual(
            out_cap.strip().splitlines()[-1],
            "action: queue orch-cap for the next batch (pool codex at cap 2/2); treat as a batch cap, not a quota wait",
        )
        self.assertFalse((orch.load_file(ap_cap) or {}).get("followups"))

        # All T3 orchestrator models exhausted -> passes through hold and records resume follow-up.
        ap_hold = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-22T08:00:00+00:00\n",
            "orch-hold.yaml",
        )
        rc_h, out_h = self._defer(
            ap_hold,
            ref="orch-hold",
            role="orchestrator",
            complexity=None,
            risk=None,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc_h, 0, out_h)
        self.assertIn("followup recorded: [resume] orch-hold", out_h)
        self.assertIn("action: hold until 2026-11-22T06:52:00+00:00", out_h.strip().splitlines()[-1])

        # Validation errors: missing --coordination, worker flags on orchestrator, --coordination on implementer.
        rc_e1, out_e1 = self._defer(ap, role="orchestrator", complexity=None, risk=None, coordination=None)
        self.assertEqual(rc_e1, 1, out_e1)
        self.assertIn("--coordination 0|1|2", out_e1)
        rc_e2, out_e2 = self._defer(ap, role="orchestrator", complexity="hard", risk="medium", coordination=2)
        self.assertEqual(rc_e2, 1, out_e2)
        self.assertIn("does not accept worker flags", out_e2)
        rc_e3, out_e3 = self._defer(ap, role="implementer", coordination=2)
        self.assertEqual(rc_e3, 1, out_e3)
        self.assertIn("--coordination only applies with --role orchestrator", out_e3)

    def test_defer_ui_fallback_defer_records_followup_and_ask_passes_through(self):
        ap_defer = self._write_assets(
            "version: 1\npreferences:\n  ui_fallback: defer\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "ui-defer.yaml",
        )
        rc, out = self._defer(
            ap_defer,
            ref="PR-ui",
            complexity="standard",
            risk="low",
            ui=True,
            available="claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("followup recorded: [resume] PR-ui (needs ui-ux-review, after 2026-11-22T06:52:00+00:00)", out)
        self.assertEqual(
            out.strip().splitlines()[-1],
            "action: hold until 2026-11-22T06:52:00+00:00 (preferred ui-ux-review exhausted); follow-up recorded: schedule its wakeup",
        )
        fus = orch.load_file(ap_defer).get("followups") or []
        self.assertEqual(len(fus), 1)
        self.assertEqual(fus[0]["needs"], "ui-ux-review")
        self.assertEqual(fus[0]["after"], "2026-11-22T06:52:00+00:00")

        ap_ask = self._write_assets(
            "version: 1\npreferences:\n  ui_fallback: ask\nproviders:\n"
            "  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-22T06:50:00+00:00\n"
            "  - id: codex\n    status: ok\n",
            "ui-ask.yaml",
        )
        rc_a, out_a = self._defer(
            ap_ask,
            ref="PR-ui-ask",
            complexity="standard",
            risk="low",
            ui=True,
            available="claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc_a, 0, out_a)
        self.assertTrue(out_a.strip().splitlines()[-1].startswith("action: ask the user once (preferred ui-ux-review exhausted"), out_a)

        # B.6: ask outcome with a capped pool notes the capped pool so the user knows the fallback is also at cap.
        rc_ac, out_ac = self._defer(
            ap_ask,
            ref="PR-ui-ask-cap",
            complexity="standard",
            risk="low",
            ui=True,
            available="claudeAgent:claude-sonnet-5-5,codex:gpt-6-2-sol",
            pool_busy="codex=2",
            pool_cap=2,
        )
        self.assertEqual(rc_ac, 0, out_ac)
        self.assertIn("note: pool(s) at cap: codex (2/2)", out_ac)
        self.assertTrue(out_ac.strip().splitlines()[-1].startswith("action: ask the user once (preferred ui-ux-review exhausted"), out_ac)

    def test_exhaust_with_selection_args_ends_with_next_dispatch_action(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: codex\n    status: ok\n"
            "  - id: claudeAgent\n    status: ok\n"
        )
        rc, out = self._exhaust(
            ap,
            "codex",
            "You've hit your usage limit · resets Nov 22, 6:50am (UTC)",
            complexity="standard",
            risk="medium",
            available="codex:gpt-6-2-sol,claudeAgent:claude-sonnet-5-5",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("codex: exhausted until 2026-11-22T06:50:00+00:00", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: dispatch claudeAgent/claude-sonnet-5-5 @ high now; choose the reviewer when its step starts")

    def test_exhaust_with_reviewer_role_dispatches_next_reviewer(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: codex\n    status: ok\n"
            "  - id: claudeAgent\n    status: ok\n"
        )
        rc, out = self._exhaust(
            ap,
            "codex",
            "You've hit your usage limit · resets Nov 22, 6:50am (UTC)",
            complexity="standard",
            risk="medium",
            available="codex:gpt-6-2-sol,claudeAgent:claude-opus-5-5,claudeAgent:claude-sonnet-5-5",
            role="reviewer",
            implementer="claudeAgent:claude-opus-5-5",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("codex: exhausted until 2026-11-22T06:50:00+00:00", out)
        self.assertIn("[note] reviewer degraded:", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(
            last,
            "action: dispatch reviewer claudeAgent/claude-sonnet-5-5 @ high now; state the reviewer degradation in the report",
        )

    def test_exhaust_without_selection_args_keeps_line_and_adds_action(self):
        ap = self._write_assets("version: 1\nproviders:\n  - id: claude\n    status: ok\n")
        rc, out = self._exhaust(ap, "claude", "You've hit your weekly limit · resets Nov 23, 3am (UTC)")
        self.assertEqual(rc, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "claude: exhausted until 2026-11-23T03:00:00+00:00")
        self.assertEqual(
            lines[-1],
            "action: run select (or defer) with the exhausted model removed and follow its action: line",
        )

    def test_exhaust_self_holds_via_orchestrator_ladder_even_under_below_tier_proceed(self):
        # B.3 + #30 integration: orchestrator ladder never provisionally drops below its tier even under below_tier: proceed.
        ap = self._write_assets(
            "version: 1\npreferences:\n  below_tier: proceed\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n"
        )
        rc, out = self._exhaust(
            ap,
            "claudeAgent",
            "You've hit your weekly limit · resets Nov 22, 6:50am (UTC)",
            is_self=True,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:claude-haiku-5-5",
        )
        self.assertEqual(rc, 4, out)
        self.assertIn("followup recorded: [resume] orchestrator-self", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "action: hold until 2026-11-22T06:52:00+00:00; schedule this conversation's resume")

    def test_exhaust_with_orchestrator_role_dispatches_next_orchestrator_ladder(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n"
        )
        rc, out = self._exhaust(
            ap,
            "claudeAgent",
            "You've hit your weekly limit · resets Nov 23, 3am (UTC)",
            role="orchestrator",
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("orchestrator tier T3", out)
        self.assertIn("ladder:", out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("action: launch the orchestrator on codex/gpt-6-2-sol @ "), out)

    def test_pattern_d_exhaust_self_switches_conversation_or_holds_and_preserves_current_task(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n"
            "followups:\n"
            "  - kind: resume\n    ref: current-task\n    needs: T4\n    after: 2026-11-25T00:00:00+00:00\n"
        )
        rc, out = self._exhaust(
            ap,
            "claudeAgent",
            "You've hit your weekly limit · resets Nov 23, 3am (UTC)",
            is_self=True,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("claudeAgent: exhausted until 2026-11-23T03:00:00+00:00", out)
        self.assertIn("orchestrator tier T3", out)
        self.assertIn("ladder:", out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(
            last,
            "action: switch this conversation to codex/gpt-6-2-sol @ medium in place, or hand off with orch.py launch <spec> "
            '(references/harnesses.md "Orchestrator\'s own quota"); log it with --role orchestrator --rubric <rubric>',
        )

        # Exhaust the last remaining provider without --ref -> defaults to orchestrator-self, preserving current-task!
        rc2, out2 = self._exhaust(
            ap,
            "codex",
            "You've hit your usage limit · resets Nov 22, 6:50am (UTC)",
            is_self=True,
            coordination=2,
            available="claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol",
            ref=None,
        )
        self.assertEqual(rc2, 4, out2)
        self.assertIn("codex: exhausted until 2026-11-22T06:50:00+00:00", out2)
        self.assertIn("followup recorded: [resume] orchestrator-self", out2)
        last2 = out2.strip().splitlines()[-1]
        self.assertEqual(last2, "action: hold until 2026-11-22T06:52:00+00:00; schedule this conversation's resume")
        by_ref = {fu["ref"]: fu for fu in (orch.load_file(ap).get("followups") or [])}
        self.assertIn("current-task", by_ref)
        self.assertEqual(by_ref["current-task"]["after"], "2026-11-25T00:00:00+00:00")
        self.assertIn("orchestrator-self", by_ref)
        self.assertEqual(by_ref["orchestrator-self"]["after"], "2026-11-22T06:52:00+00:00")

    def test_defer_and_exhaust_error_paths_and_silently_dropped_args(self):
        ap = self._write_assets("version: 1\nproviders:\n  - id: claude\n    status: ok\n")
        # defer --role reviewer without --implementer
        rc, out = self._defer(ap, role="reviewer", implementer=None)
        self.assertEqual(rc, 1, out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("action: rerun defer --role reviewer"), out)

        # defer --pool-cap 0
        rc, out = self._defer(ap, pool_cap=0)
        self.assertEqual(rc, 1, out)
        self.assertIn("--pool-cap", out)

        # defer --pool-busy with bare integer instead of pool=N
        rc, out = self._defer(ap, pool_busy="2")
        self.assertEqual(rc, 1, out)
        self.assertIn("--pool-busy", out)

        # N3: defer --pool-busy with unknown provider (e.g. family name) or typo group name -> error rc=1
        rc, out = self._defer(
            ap,
            available="claude:claude-opus-5-5",
            pool_busy="anthropic=2",
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("no entry in --available", out)

        ap_grp = self._write_assets(
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    status: ok\n    groups:\n"
            "      - name: Gemini Models\n        models: [gemini-4-neon]\n        status: ok\n        window: 7d\n",
            "err-grp.yaml",
        )
        rc, out = self._defer(
            ap_grp,
            available="antigravity:gemini-4-neon",
            pool_busy="antigravity/Gemnii=2",
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("does not match any quota group", out)

        # N6/B.1: defer --user-hold with invalid --needs (unknown target, bad group on listed provider, bare any) -> error rc=1
        for bad_needs in ("claud", "provider:claude/T9", "any"):
            rc, out = self._defer(
                ap,
                user_hold=True,
                complexity=None,
                risk=None,
                available=None,
                needs=bad_needs,
            )
            self.assertEqual(rc, 1, (bad_needs, out))
            self.assertTrue(out.strip().splitlines()[-1].startswith("action: rerun defer --user-hold"), out)
        rc, out = self._defer(
            ap_grp,
            user_hold=True,
            complexity=None,
            risk=None,
            available=None,
            needs="provider:antigravity/Gemnii/T4",
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("unknown quota group", out)

        # defer --needs without --user-hold (silently dropped arg -> error)
        rc, out = self._defer(ap, needs="provider:claude/T3", user_hold=False)
        self.assertEqual(rc, 1, out)
        self.assertIn("--user-hold", out)

        # exhaust with partial selection args
        rc, out = self._exhaust(ap, "claude", "hit limit · resets Nov 23, 3am (UTC)", complexity="standard")
        self.assertEqual(rc, 1, out)
        self.assertIn("--complexity, --risk, and --available", out)

        # exhaust with --ref / --ui / --below-tier without core selection args (silently dropped -> error)
        for extra in ({"ref": "item-1"}, {"ui": True}, {"below_tier": "wait"}):
            rc, out = self._exhaust(ap, "claude", "hit limit · resets Nov 23, 3am (UTC)", **extra)
            self.assertEqual(rc, 1, (extra, out))
            self.assertTrue(out.strip().splitlines()[-1].startswith("action: rerun exhaust"), out)

        # exhaust --self without --coordination/--available or with worker flags
        rc, out = self._exhaust(ap, "claude", "hit limit · resets Nov 23, 3am (UTC)", is_self=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("exhaust --self requires --coordination 0|1|2 and --available", out)

        rc, out = self._exhaust(
            ap, "claude", "hit limit · resets Nov 23, 3am (UTC)", is_self=True, available="claude:claude-opus-5-5"
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("exhaust --self requires --coordination 0|1|2 and --available", out)

        rc, out = self._exhaust(
            ap,
            "claude",
            "hit limit · resets Nov 23, 3am (UTC)",
            is_self=True,
            complexity="hard",
            risk="medium",
            available="claude:claude-opus-5-5",
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("omit worker flags", out)

    def test_exhaust_rejects_malformed_available_before_writing_lockout(self):
        text = "version: 1\nproviders:\n  - id: claudeAgent\n    status: ok\n  - id: codex\n    status: ok\n"
        ap = self._write_assets(text, "bad-avail.yaml")
        msg = "You've hit your weekly limit · resets Nov 23, 3am (UTC)"
        for kw in (
            dict(complexity="standard", risk="low"),
            dict(is_self=True, coordination=2),
            dict(role="orchestrator", coordination=2),
        ):
            rc, out = self._exhaust(ap, "claudeAgent", msg, available="claudeAgent claude-opus-5-5,codex:gpt-6-2-sol", **kw)
            self.assertEqual(rc, 1, (kw, out))
            self.assertIn("error: --available entries are not provider:model: ", out)
            self.assertNotIn("exhausted until", out)
            self.assertTrue(out.strip().splitlines()[-1].endswith("and rerun exhaust; nothing was recorded"), out)
            with open(ap, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), text, kw)

    def test_defer_and_exhaust_self_keep_select_orchestrator_action_suffixes(self):
        text = (
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    status: ok\n"
            "  - id: claudeAgent\n    status: ok\n"
            "  - id: codex\n    status: ok\n"
        )
        avail = "antigravity:gemini-4-argon,claudeAgent:claude-opus-5-5,codex:gpt-6-2-sol"
        ap_sel = self._write_assets(text, "sfx-select.yaml")
        rc_s, out_s = _run_orch(
            orch.select,
            _Args(
                role="orchestrator", complexity=None, risk=None, coordination=2, available=avail, ui=False,
                prefer_family=None, fallback=None, below_tier=None, ref="orch-sfx", fail_on_low=False, implementer=None,
                catalog=CATALOG_PATH, assets=ap_sel, now=self.NOW, full=False,
            ),
        )
        self.assertEqual(rc_s, 0, out_s)
        sel_last = out_s.strip().splitlines()[-1]
        sfx = "; ask the user before the provider switch; log it with --role orchestrator --rubric <rubric>"
        self.assertTrue(sel_last.endswith(sfx), out_s)
        # defer and exhaust --self name the phase-boundary rung, so the clause does not read as a gate on the self-switch
        sw_prefix = "provider switch (phase boundary, ask the user first): "
        sw = next(l for l in out_s.splitlines() if l.startswith(sw_prefix))[len(sw_prefix):]
        named = f"; ask the user before the phase-boundary provider switch to {sw}; log it with --role orchestrator --rubric <rubric>"

        ap_def = self._write_assets(text, "sfx-defer.yaml")
        rc_d, out_d = self._defer(
            ap_def, ref="orch-sfx", role="orchestrator", complexity=None, risk=None, coordination=2, available=avail
        )
        self.assertEqual(rc_d, 5, out_d)
        def_last = out_d.strip().splitlines()[-1]
        self.assertTrue(def_last.endswith("; do not defer orch-sfx" + named), out_d)
        self.assertEqual(def_last.split(";")[0], sel_last.split(";")[0])

        ap_ex = self._write_assets(text, "sfx-exhaust.yaml")
        rc_e, out_e = self._exhaust(
            ap_ex, "claudeAgent", "You've hit your weekly limit · resets Nov 23, 3am (UTC)",
            is_self=True, coordination=2, available=avail,
        )
        self.assertEqual(rc_e, 0, out_e)
        self.assertIn("provider switch (phase boundary, ask the user first): ", out_e)
        ex_last = out_e.strip().splitlines()[-1]
        self.assertTrue(ex_last.startswith("action: switch this conversation to "), out_e)
        self.assertIn("orch.py launch <spec>", ex_last)
        self.assertTrue(ex_last.endswith(named), out_e)

    def test_defer_user_hold_notes_needs_that_never_become_due(self):
        ap = self._write_assets(
            "version: 1\nproviders:\n  - id: claudeAgent\n    status: ok\n  - id: antigravity\n    status: ok\n", "never-due.yaml"
        )
        cases = (
            ("emg-scoped", "provider:claudeAgent/claude-fable-5-2", "note: --needs claude-fable-5-2 is an emergency model"),
            ("emg-bare", "GPT-6.2 Astra", "note: --needs gpt-6-2-astra is an emergency model"),
            ("unrated", "provider:antigravity/gemini-pro-orchestrator-neon", "note: --needs gemini-pro-orchestrator-neon is unrated"),
        )
        for ref, needs, note in cases:
            rc, out = self._defer(ap, ref=ref, user_hold=True, complexity=None, risk=None, available=None, needs=needs)
            self.assertEqual(rc, 0, out)
            self.assertIn(note + ", so this follow-up never becomes due", out)
            self.assertTrue(out.strip().splitlines()[-1].startswith(f"action: hold {ref} as the user asked"), out)
        for ref, needs in (("rated", "provider:claudeAgent/claude-opus-5-5"), ("tier", "provider:claudeAgent/T3"), ("family", "anthropic")):
            rc, out = self._defer(ap, ref=ref, user_hold=True, complexity=None, risk=None, available=None, needs=needs)
            self.assertEqual(rc, 0, out)
            self.assertNotIn("never becomes due", out)

    def test_usage_lists_exhaust_role_orchestrator_form(self):
        usage = orch.__doc__.split("Exit codes")[0]
        exhaust_usage = usage[usage.index("orch.py exhaust"):usage.index("orch.py status")]
        self.assertIn("--role orchestrator --coordination 0|1|2 --available ...", exhaust_usage)
        self.assertIn("--self --coordination 0|1|2 --available ...", exhaust_usage)


class TestLaunchSpec(unittest.TestCase):
    """`orch.py launch <spec>`: validation, effort option keys, harness model ids, headers, overrides, roles, non-dispatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = os.path.join(self.tmp.name, "assets.yaml")
        _write(
            self.assets,
            "version: 1\nproviders:\n"
            "  - id: antigravity\n    policy: normal\n    status: ok\n"
            "  - id: claudeAgent\n    policy: normal\n    status: ok\n"
            "  - id: codex\n    policy: spend-first\n    status: ok\n",
        )
        self.brief = os.path.join(self.tmp.name, "brief.md")
        _write(self.brief, "GOAL: Ship the orchestrator launch spec.\n")

    def _launch(self, spec_text, name="task.launch.yaml", available=None, out=None, assets=None, full=False, no_probe=True):
        spec_path = os.path.join(self.tmp.name, name)
        _write(spec_path, spec_text)
        args = _Args(
            available=available,
            no_probe=no_probe,
            out=out,
            assets=assets or self.assets,
            catalog=CATALOG_PATH,
            now="2026-11-20T21:00:00Z",
            full=full,
        )
        args.spec = spec_path
        return _run(orch.launch_cmd, args)

    def test_validation_collects_multiple_errors_in_one_run(self):
        bad_spec = (
            "version: 1\n"
            "unknown_top: 123\n"
            "role: reviewer\n"
            "brief: missing-brief.md\n"
            "rubric: {size: 2, uncertainty: 5}\n"
            "ladder: [not-a-pme]\n"
            "available: [codex:gpt-6.2-sol]\n"
            "workspace: {strategy: worktree, children: invalid_child}\n"
            "caps: {max_escalations: 4, max_children_per_pool: -1, budget: {codex: -2}}\n"
        )
        rc, out = self._launch(bad_spec, name="broken.launch.yaml")
        self.assertEqual(rc, 1, out)
        lines = out.strip().splitlines()
        for expected in (
            "unknown_top",
            "brief",
            "rubric",
            "ladder",
            "implementer",
            "workspace.base",
            "workspace.children",
            "caps.max_escalations",
            "caps.max_children_per_pool",
            "caps.budget",
        ):
            self.assertIn(expected, out)
        self.assertIn("error: 10 problem(s); action: fix ", lines[-1])
        self.assertTrue(lines[-1].endswith("action: fix " + os.path.join(self.tmp.name, "broken.launch.yaml") + " and rerun launch"), lines[-1])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "broken.payload.json")))

        # Empty brief file and bad role also flagged
        empty_brief = os.path.join(self.tmp.name, "empty.md")
        _write(empty_brief, "   \n")
        rc2, out2 = self._launch(
            "version: 1\nrole: boss\nbrief: empty.md\n"
            "rubric: {size: 1, uncertainty: 1, deps: 1, consequences: 1, verification: 1, coordination: 1}\n"
            "available: [codex:gpt-6.2-sol]\nworkspace: {strategy: existing_worktree}\n",
            name="bad2.launch.yaml",
        )
        self.assertEqual(rc2, 1, out2)
        self.assertIn("role", out2)
        self.assertIn("empty.md", out2)
        self.assertIn("worktree_path", out2)

    def test_orchestrator_payload_codex_service_tier_harness_id_and_header(self):
        spec = (
            "version: 1\n"
            "role: orchestrator\n"
            'title: "agent-orchestration: orchestrator role PRs"\n'
            "repo: osnoser1/agent-orchestration\n"
            "brief: brief.md\n"
            "rubric: {size: 2, uncertainty: 1, deps: 2, consequences: 1, verification: 1, coordination: 2}\n"
            "ladder: auto\n"
            "available: [codex:gpt-6.2-sol, claudeAgent:claude-opus-5-5]\n"
            "workspace: {strategy: worktree, base: origin/main, children: shared}\n"
            "caps: {max_escalations: 2, max_children_per_pool: 2, budget: {antigravity: 10, claudeAgent: 4}}\n"
        )
        rc, out = self._launch(spec, name="orch.launch.yaml")
        self.assertEqual(rc, 0, out)
        payload_path = os.path.join(self.tmp.name, "orch.payload.json")
        self.assertTrue(os.path.isfile(payload_path))
        self.assertIn("role: orchestrator · hard/medium · tier T3 (coordination 2)", out)
        self.assertIn("requested: codex:gpt-6.2-sol@medium", out)
        self.assertIn("ladder: codex/gpt-6.2-sol @ medium -> codex/gpt-6.2-sol @ high ; provider switch: claudeAgent/claude-opus-5-5 @ high", out)
        self.assertIn(f"payload: {payload_path}", out)
        expected_log = (
            "log: orch.py log --event select --surface conversation --repo agent-orchestration --role orchestrator "
            "--complexity hard --risk medium --provider codex --model gpt-6.2-sol --effort medium "
            "--rubric size=2,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2 "
            "--requested codex:gpt-6.2-sol@medium --effective <P:M@E>"
        )
        self.assertIn(expected_log, out)
        self.assertEqual(
            out.strip().splitlines()[-1],
            f"action: call t3_thread_launch with the JSON in {payload_path}; then read the new thread's model "
            "(t3_thread_configure with only threadId) and run the log: line above with --effective <P:M@E> filled in",
        )

        # Parse the emitted log: line, fill in <what the thread reports>, and run orch.py log with it
        log_line = next(l for l in out.splitlines() if l.startswith("log: orch.py log "))
        filled = log_line[len("log: orch.py "):].replace("<P:M@E>", "codex:gpt-6.2-sol@medium")
        ledger_path = os.path.join(self.tmp.name, "parsed-ledger.jsonl")
        parsed_log_args = orch.build_parser().parse_args(filled.split() + ["--ledger", ledger_path, "--assets", self.assets, "--now", "2026-11-20T21:00:00Z"])
        rc_l, out_l = _run(orch.log_cmd, parsed_log_args)
        self.assertEqual(rc_l, 0, out_l)
        with open(ledger_path, encoding="utf-8") as fh:
            logged_row = json.loads(fh.read().strip())
        self.assertEqual(logged_row["repo"], "agent-orchestration")
        self.assertEqual(logged_row["role"], "orchestrator")
        self.assertEqual(logged_row["requested"], "codex:gpt-6-2-sol@medium")
        self.assertEqual(logged_row["effective"], "codex:gpt-6-2-sol@medium")

        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["title"], "agent-orchestration: orchestrator role PRs")
        self.assertEqual(
            payload["modelSelection"],
            {
                "instanceId": "codex",
                "model": "gpt-6.2-sol",
                "options": [
                    {"id": "reasoningEffort", "value": "medium"},
                    {"id": "serviceTier", "value": "default"},
                ],
            },
        )
        self.assertEqual(
            payload["workspaceStrategy"],
            {"type": "worktree", "baseRef": "origin/main", "startFromOrigin": True},
        )
        self.assertEqual(
            payload["clientRequestId"],
            "launch-agent-orchestration-orchestrator-role-prs-codex-gpt-6.2-sol-medium-202611202100",
        )
        msg = payload["message"]
        header, sep, body = msg.partition("\n\n")
        self.assertEqual(sep, "\n\n")
        self.assertEqual(body, "GOAL: Ship the orchestrator launch spec.\n")
        hlines = header.splitlines()
        self.assertTrue(all(len(l) <= 200 for l in hlines), header)
        self.assertEqual(
            hlines,
            [
                "ORCHESTRATOR — repeat this block, updated, at every phase boundary",
                "model: codex/gpt-6.2-sol @ medium (rung 1 of 2)",
                "ladder: codex/gpt-6.2-sol @ medium -> codex/gpt-6.2-sol @ high",
                "provider switch (phase boundary, ask the user first): claudeAgent/claude-opus-5-5 @ high",
                "escalate on (orch.py escalate): outcome rework | 3+ review findings your brief did not flag (workers: 3+) | a child's STATUS: ESCALATE; "
                "at most 2 escalations; never step down mid-phase",
                "caps: children per pool 2; budget antigravity 10, claudeAgent 4",
                "workspace: worktree from origin/main; children shared",
                "rubric: size=2,uncertainty=1,deps=2,consequences=1,verification=1,coordination=2 (hard/medium)",
                "verify: the launcher compares requested vs effective model/effort after launch (references/orchestrator-launch.md)",
            ],
        )

    def test_implementer_and_reviewer_option_keys_and_headers(self):
        # Implementer on antigravity -> thought_level, no ladder/switch/escalate lines
        impl_spec = (
            "version: 1\n"
            "role: implementer\n"
            'title: "impl worker"\n'
            "brief: brief.md\n"
            "rubric: {size: 1, uncertainty: 1, deps: 1, consequences: 0, verification: 0, coordination: 0}\n"
            "available: [antigravity:gemini-4-neon]\n"
            "workspace: {strategy: root}\n"
        )
        out_impl = os.path.join(self.tmp.name, "custom-impl.json")
        rc, out = self._launch(impl_spec, name="impl.launch.yaml", out=out_impl)
        self.assertEqual(rc, 0, out)
        with open(out_impl, encoding="utf-8") as fh:
            p_impl = json.load(fh)
        self.assertEqual(
            p_impl["modelSelection"],
            {"instanceId": "antigravity", "model": "gemini-4-neon", "options": [{"id": "thought_level", "value": "low"}]},
        )
        self.assertEqual(p_impl["workspaceStrategy"], {"type": "root"})
        h_impl = p_impl["message"].partition("\n\n")[0].splitlines()
        self.assertEqual(h_impl[0], "IMPLEMENTER — repeat this block, updated, at every phase boundary")
        self.assertEqual(h_impl[1], "model: antigravity/gemini-4-neon @ low")
        for forbidden in ("ladder:", "provider switch", "escalate on:", "caps:"):
            self.assertFalse(any(l.startswith(forbidden) for l in h_impl), h_impl)

        # Reviewer on claudeAgent_claude_extra -> effort, existing_worktree
        rev_spec = (
            "version: 1\n"
            "role: reviewer\n"
            'title: "cross-family review"\n'
            "brief: brief.md\n"
            "rubric: {size: 2, uncertainty: 1, deps: 1, consequences: 1, verification: 1, coordination: 1}\n"
            "implementer: antigravity:gemini-4-neon\n"
            "available: [antigravity:gemini-4-neon, claudeAgent_claude_extra:claude-opus-5-5]\n"
            "workspace: {strategy: existing_worktree, worktree_path: /tmp/wt}\n"
        )
        rc_r, out_r = self._launch(rev_spec, name="rev.launch.yaml")
        self.assertEqual(rc_r, 0, out_r)
        with open(os.path.join(self.tmp.name, "rev.payload.json"), encoding="utf-8") as fh:
            p_rev = json.load(fh)
        self.assertEqual(
            p_rev["modelSelection"],
            {"instanceId": "claudeAgent_claude_extra", "model": "claude-opus-5-5", "options": [{"id": "effort", "value": "high"}]},
        )
        self.assertEqual(p_rev["workspaceStrategy"], {"type": "existing_worktree", "worktreePath": "/tmp/wt"})
        h_rev = p_rev["message"].partition("\n\n")[0].splitlines()
        self.assertEqual(h_rev[0], "REVIEWER — repeat this block, updated, at every phase boundary")

        # Unknown provider without effort_key fails with clear error; setting effort_key in assets succeeds
        unk_spec = (
            "version: 1\nrole: implementer\ntitle: unk\nbrief: brief.md\n"
            "rubric: {size: 1, uncertainty: 0, deps: 0, consequences: 0, verification: 0, coordination: 0}\n"
            "available: [customProv:gemini-4-neon]\nworkspace: {strategy: root}\n"
        )
        rc_u, out_u = self._launch(unk_spec, name="unk.launch.yaml")
        self.assertEqual(rc_u, 1, out_u)
        self.assertIn("no effort option key for customProv: set effort_key on it in assets.yaml", out_u)

        ap_custom = os.path.join(self.tmp.name, "custom-assets.yaml")
        _write(ap_custom, "version: 1\nproviders:\n  - id: customProv\n    effort_key: custom_effort\n    status: ok\n")
        rc_u2, out_u2 = self._launch(unk_spec, name="unk.launch.yaml", assets=ap_custom)
        self.assertEqual(rc_u2, 0, out_u2)
        with open(os.path.join(self.tmp.name, "unk.payload.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["modelSelection"]["options"], [{"id": "custom_effort", "value": "low"}])

    def test_spec_ladder_and_max_escalations_override_preferences(self):
        ap = os.path.join(self.tmp.name, "prefs-orch.yaml")
        _write(
            ap,
            "version: 1\npreferences:\n  orchestrator:\n"
            "    max_escalations: 2\n"
            "    ladder: [antigravity:gemini-4-argon@high, antigravity:gemini-4-neon@medium, antigravity:gemini-4-neon@high]\n"
            "providers:\n  - id: antigravity\n    status: ok\n  - id: codex\n    status: ok\n",
        )
        spec = (
            "version: 1\nrole: orchestrator\ntitle: override\nbrief: brief.md\n"
            "rubric: {size: 2, uncertainty: 1, deps: 1, consequences: 1, verification: 1, coordination: 2}\n"
            "ladder: [codex:gpt-6.2-sol@medium, codex:gpt-6.2-sol@high, antigravity:gemini-4-neon@high]\n"
            "caps: {max_escalations: 0}\n"
            "workspace: {strategy: worktree, base: main}\n"
        )
        # CLI --available overrides spec available; max_escalations=0 caps same-provider ladder to 1 rung; base=main has startFromOrigin=False
        rc, out = self._launch(
            spec,
            name="ovr.launch.yaml",
            available="codex:gpt-6.2-sol,antigravity:gemini-4-neon",
            assets=ap,
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("requested: codex:gpt-6.2-sol@medium", out)
        self.assertIn("ladder: codex/gpt-6.2-sol @ medium ; provider switch: antigravity/gemini-4-neon @ high", out)
        with open(os.path.join(self.tmp.name, "ovr.payload.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["workspaceStrategy"], {"type": "worktree", "baseRef": "main", "startFromOrigin": False})
        self.assertIn("model: codex/gpt-6.2-sol @ medium (rung 1 of 1)", payload["message"])
        self.assertIn("at most 0 escalations", payload["message"])

    def test_non_dispatch_outcome_exits_3_and_writes_no_payload(self):
        ap_ex = os.path.join(self.tmp.name, "ex-assets.yaml")
        _write(
            ap_ex,
            "version: 1\nproviders:\n"
            "  - id: codex\n    status: exhausted\n    exhausted_until: 2026-11-25T00:00:00+00:00\n",
        )
        spec = (
            "version: 1\nrole: orchestrator\ntitle: blocked\nbrief: brief.md\n"
            "rubric: {size: 2, uncertainty: 1, deps: 2, consequences: 1, verification: 1, coordination: 2}\n"
            "available: [codex:gpt-6.2-sol]\nworkspace: {strategy: root}\n"
        )
        rc, out = self._launch(spec, name="blocked.launch.yaml", assets=ap_ex)
        self.assertEqual(rc, 3, out)
        self.assertIn("action: run select --role orchestrator --coordination 2 --available codex:gpt-6.2-sol and follow its action line; launch only resolves a dispatchable model", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "blocked.payload.json")))

        # Implementer that would stage (tier-meeting exhausted, below-tier available) also exits 3 and writes no payload
        impl_stage = (
            "version: 1\nrole: implementer\ntitle: stage-blocked\nbrief: brief.md\n"
            "rubric: {size: 2, uncertainty: 2, deps: 2, consequences: 2, verification: 1, coordination: 0}\n"
            "available: [codex:gpt-6.2-sol, antigravity:gemini-4-argon]\nworkspace: {strategy: root}\n"
        )
        rc2, out2 = self._launch(impl_stage, name="stage.launch.yaml", assets=ap_ex)
        self.assertEqual(rc2, 3, out2)
        self.assertIn("launch only resolves a dispatchable model", out2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "stage.payload.json")))

    def test_degraded_reviewer_notes_header_and_action(self):
        rev_deg = (
            "version: 1\nrole: reviewer\ntitle: same-model review\nrepo: osnoser1/agent-orchestration\nbrief: brief.md\n"
            "rubric: {size: 1, uncertainty: 1, deps: 1, consequences: 0, verification: 0, coordination: 0}\n"
            "implementer: codex:gpt-6.2-sol\n"
            "available: [codex:gpt-6.2-sol]\nworkspace: {strategy: root}\n"
        )
        rc, out = self._launch(rev_deg, name="deg-rev.launch.yaml")
        self.assertEqual(rc, 0, out)
        self.assertIn("[note] reviewer degraded: no other model usable; using the implementer's model in a fresh conversation. State this in the report.", out)
        self.assertTrue(out.strip().splitlines()[-1].endswith("; state the reviewer degradation in the report"), out)
        with open(os.path.join(self.tmp.name, "deg-rev.payload.json"), encoding="utf-8") as fh:
            p_deg = json.load(fh)
        h_deg = p_deg["message"].partition("\n\n")[0].splitlines()
        self.assertTrue(any(l.startswith("reviewer degraded: ") and l.endswith("; state it in the report") for l in h_deg), h_deg)

    def test_no_assets_probe_skip_probe_failure_note_unwritable_out_stale_cleanup_and_ignored_keys(self):
        spec = (
            "version: 1\nrole: implementer\ntitle: worker\nbrief: brief.md\n"
            "rubric: {size: 1, uncertainty: 0, deps: 0, consequences: 0, verification: 0, coordination: 0}\n"
            "ladder: auto\ncaps: {max_escalations: 1}\n"
            "available: [claudeAgent:claude-sonnet-5-5]\nworkspace: {strategy: root}\n"
        )
        # 1. Missing assets file without --no-probe -> skips probe with note, plus notes ignored ladder/max_escalations for implementer
        missing_assets = os.path.join(self.tmp.name, "no-such-assets.yaml")
        rc1, out1 = self._launch(spec, name="w1.launch.yaml", assets=missing_assets, no_probe=False)
        self.assertEqual(rc1, 0, out1)
        self.assertIn("note: no assets file: probe skipped (run orch.py probe --init)", out1)
        self.assertIn("note: ignored for role implementer: caps.max_escalations", out1)  # `ladder: auto` is the default
        self.assertFalse(os.path.exists(missing_assets))

        # 2. Existing assets file whose providers have no probe settings, run WITHOUT --no-probe -> succeeds
        no_probe_cfg_assets = os.path.join(self.tmp.name, "no-probe-cfg.yaml")
        _write(no_probe_cfg_assets, "version: 1\nproviders:\n  - id: claudeAgent\n    status: ok\n")
        rc2, out2 = self._launch(spec, name="w2.launch.yaml", assets=no_probe_cfg_assets, no_probe=False)
        self.assertEqual(rc2, 0, out2)
        self.assertNotIn("probe failed", out2)

        # 3. Probe raises or exits non-zero -> prints note: probe failed (...); resolving on the last known quota
        with mock.patch("orch._probe", side_effect=RuntimeError("boom")):
            rc3, out3 = self._launch(spec, name="w3.launch.yaml", assets=no_probe_cfg_assets, no_probe=False)
        self.assertEqual(rc3, 0, out3)
        self.assertIn("note: probe failed (boom); resolving on the last known quota", out3)

        # 4. --out pointing to a directory -> clean error, exit 1, no traceback
        rc4, out4 = self._launch(spec, name="w4.launch.yaml", out=self.tmp.name)
        self.assertEqual(rc4, 1, out4)
        self.assertIn(f"error: cannot write payload to {self.tmp.name}:", out4)
        self.assertTrue(out4.strip().splitlines()[-1].startswith("error: 1 problem(s); action:"), out4)

        # 4b. A mistyped --out that names the spec itself is never deleted (only launch payload JSON is)
        spec_path = os.path.join(self.tmp.name, "w1.launch.yaml")
        ex_assets0 = os.path.join(self.tmp.name, "ex-only0.yaml")
        _write(ex_assets0, "version: 1\nproviders:\n  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-25T00:00:00+00:00\n")
        rc4b, out4b = self._launch(spec, name="w1.launch.yaml", assets=ex_assets0, out=spec_path)
        self.assertEqual(rc4b, 3, out4b)
        self.assertTrue(os.path.isfile(spec_path))
        self.assertNotIn("removed stale payload", out4b)

        # 5. Stale payload at --out is deleted on exit 3 (and on validation error) with a note line
        stale_payload = os.path.join(self.tmp.name, "w1.payload.json")
        self.assertTrue(os.path.isfile(stale_payload))
        ex_assets = os.path.join(self.tmp.name, "ex-only.yaml")
        _write(ex_assets, "version: 1\nproviders:\n  - id: claudeAgent\n    status: exhausted\n    exhausted_until: 2026-11-25T00:00:00+00:00\n")
        rc5, out5 = self._launch(spec, name="w1.launch.yaml", assets=ex_assets)
        self.assertEqual(rc5, 3, out5)
        self.assertFalse(os.path.exists(stale_payload))
        self.assertIn(f"note: removed stale payload {stale_payload}", out5)

        # 6. check rejects bad provider effort_key
        bad_ek = os.path.join(self.tmp.name, "bad-ek.yaml")
        _write(bad_ek, "version: 1\nproviders:\n  - id: customProv\n    effort_key: 123\n")
        rc6, out6 = _run(orch.check, _Args(assets=bad_ek, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile=""))
        self.assertEqual(rc6, 1, out6)
        self.assertIn("effort_key", out6)



class TestQualityEscalation(unittest.TestCase):
    """`orch.py escalate`, `log --unflagged`, `select --role orchestrator --phase`, `history --by`, and their preferences."""

    PROVIDERS = (
        "providers:\n"
        "  - id: antigravity\n    status: ok\n"
        "  - id: claude\n    status: ok\n"
        "  - id: claudeAgent\n    status: ok\n"
        "  - id: codex\n    status: ok\n"
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.assets = os.path.join(self.dir, "assets.yaml")
        self.ledger = os.path.join(self.dir, "ledger.jsonl")
        _write(self.assets, "version: 1\n" + self.PROVIDERS)

    def prefs(self, text):
        ap = os.path.join(self.dir, "prefs.yaml")
        _write(ap, "version: 1\npreferences:\n" + text + self.PROVIDERS)
        return ap

    def esc(self, current, available, cause="rework", role="implementer", ref="PR-7", **kw):
        base = dict(
            assets=self.assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", ledger=self.ledger, role=role, ref=ref,
            current=current, cause=cause, available=available, findings=None, complexity=None, risk=None,
            coordination=None, implementer=None, dry_run=False, repo="demo-app", full=False, approved=False, to=None,
        )
        if role in ("implementer", "reviewer"):
            base.update(complexity="standard", risk="low")
        else:
            base.update(coordination=2)
        base.update(kw)
        rc, out = _run(orch.escalate_cmd, _Args(**base))
        return rc, out.rstrip("\n").split("\n")

    def rows(self):
        if not os.path.exists(self.ledger):
            return []
        with open(self.ledger, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def seed(self, *entries):
        with open(self.ledger, "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    # ---------- escalate: rungs ----------
    def test_effort_step_first_logs_the_row_with_its_cause(self):
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol,claude:claude-opus-5-5")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, [
            "escalate: implementer codex/gpt-6-2-sol @ medium -> codex/gpt-6-2-sol @ high (cause rework; escalation 1 of 2)",
            "action: switch the implementer in place to codex/gpt-6-2-sol @ high (references/harnesses.md, Switch model in place) "
            "and continue; re-review after the change",
        ])
        self.assertEqual(self.rows(), [{
            "ts": "2026-11-20T21:00:00+00:00", "event": "escalate", "repo": "demo-app", "role": "implementer", "ref": "PR-7",
            "from": "codex:gpt-6-2-sol@medium", "to": "codex:gpt-6-2-sol@high", "cause": "rework",
        }])

    def test_model_step_prefers_the_same_provider_then_crosses_providers(self):
        # sonnet@xhigh has no higher non-emergency effort: the same-provider opus@high (51.84 > 49.92) beats the better-ranked neon
        rc, lines = self.esc("claude:claude-sonnet-5-5@xhigh", "claude:claude-sonnet-5-5,claude:claude-opus-5-5,antigravity:gemini-4-neon")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], "escalate: implementer claude/claude-sonnet-5-5 @ xhigh -> claude/claude-opus-5-5 @ high (cause rework; escalation 1 of 2)")
        # no same-provider model scores higher than sol@xhigh (55.74): workers cross providers without asking
        rc2, lines2 = self.esc("codex:gpt-6-2-sol@xhigh", "codex:gpt-6-2-sol,codex:gpt-6-1-terra,antigravity:gemini-4-neon", ref="PR-8")
        self.assertEqual(rc2, 0, lines2)
        self.assertEqual(lines2[-1], "action: switch the implementer in place to antigravity/gemini-4-neon @ high "
                                     "(references/harnesses.md, Switch model in place) and continue; re-review after the change")
        self.assertEqual(self.rows()[-1]["to"], "antigravity:gemini-4-neon@high")

    def test_model_step_when_the_current_model_is_no_longer_usable(self):
        ap = os.path.join(self.dir, "codex-out.yaml")
        _write(ap, "version: 1\nproviders:\n  - id: antigravity\n    status: ok\n  - id: codex\n    status: exhausted\n"
                   "    exhausted_until: 2026-11-23T08:00:00+00:00\n")
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol,antigravity:gemini-4-neon", assets=ap)
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> antigravity/gemini-4-neon @ medium (cause rework", lines[0])  # lowest neon effort above sol@medium (52.68)

    def test_never_steps_down_and_reports_when_no_higher_rung_exists(self):
        rc, lines = self.esc("antigravity:gemini-4-neon@xhigh", "antigravity:gemini-4-neon,claude:claude-opus-5-5,codex:gpt-6-2-sol")
        self.assertEqual(rc, 2, lines)
        self.assertEqual(lines[-1], "action: no higher rung: report it to the user with the evidence")
        self.assertEqual(self.rows(), [])
        for current, avail in (("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol,claude:claude-haiku-5-5,codex:gpt-6-luna"),
                               ("claude:claude-sonnet-5-5@high", "claude:claude-sonnet-5-5,codex:gpt-6-1-terra,antigravity:gemini-4-neon")):
            target = orch.escalation_target(
                orch.load_catalog_with_local(CATALOG_PATH, self.assets), orch.load_file(self.assets), avail.split(","),
                dict(orch.FLOORS), "T2", orch.get_now("2026-11-20T21:00:00Z"), orch._parse_pme(current),
            )
            self.assertIsNotNone(target, current)
            self.assertGreater(target["score"], target["from_score"], current)

    def test_emergency_only_higher_rung_asks_the_user_once(self):
        asks = []
        for current, avail in (("claude:claude-opus-5-5@xhigh", "claude:claude-opus-5-5,codex:gpt-6-2-sol"),
                               ("antigravity:gemini-4-neon@xhigh", "antigravity:gemini-4-neon,codex:gpt-6-2-astra")):
            rc, lines = self.esc(current, avail)
            self.assertEqual(rc, 3, lines)
            self.assertTrue(lines[-1].startswith("action: ask the user once (only emergency models or efforts are higher: "), lines)
            self.assertIn("rerun this escalate with --approved --to ", lines[-1])
            asks.append(lines[-1])
        self.assertTrue(asks[0].endswith("higher: claude:claude-opus-5-5@max); once approved, rerun this escalate with --approved --to claude:claude-opus-5-5@max"), asks)
        self.assertIn("--approved --to codex:gpt-6-2-astra@<effort>", asks[1])
        self.assertEqual(self.rows(), [])

    def test_reviewer_never_lands_on_the_implementer_model(self):
        rc, lines = self.esc("claude:claude-sonnet-5-5@xhigh", "claude:claude-sonnet-5-5,claude:claude-opus-5-5,antigravity:gemini-4-neon",
                             role="reviewer", implementer="claude:claude-opus-5-5")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], "escalate: reviewer claude/claude-sonnet-5-5 @ xhigh -> antigravity/gemini-4-neon @ medium (cause rework; escalation 1 of 2)")
        rc2, lines2 = self.esc("claude:claude-sonnet-5-5@xhigh", "claude:claude-sonnet-5-5,claude:claude-opus-5-5",
                               role="reviewer", implementer="claude-opus-5-5", ref="PR-9")
        self.assertEqual(rc2, 3, lines2)  # only sonnet@max (emergency) is higher once the implementer's opus is excluded
        self.assertEqual(self.rows()[-1]["ref"], "PR-7")

    # ---------- escalate: triggers and cap ----------
    def test_triggers_and_below_threshold(self):
        cur, avail = "codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol"
        rc, lines = self.esc(cur, avail, cause="findings", findings=2)
        self.assertEqual((rc, lines), (0, ["action: no escalation (findings below threshold: 2 < 3); continue on codex/gpt-6-2-sol @ medium"]))
        self.assertEqual(self.rows(), [])
        rc, lines = self.esc(cur, avail, cause="findings", findings=3)
        self.assertEqual(rc, 0, lines)
        self.assertIn("(cause findings; escalation 1 of 2)", lines[0])
        self.assertEqual(self.rows()[-1]["findings"], 3)
        rc, lines = self.esc(cur, avail, cause="status", ref="PR-8")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.rows()[-1]["cause"], "status")
        self.assertNotIn("findings", self.rows()[-1])
        # configurable thresholds: workers read preferences.escalation.findings, the orchestrator unflagged_findings
        ap = self.prefs("  escalation: {findings: 5, max: 2}\n  orchestrator:\n    unflagged_findings: 2\n")
        rc, lines = self.esc(cur, avail, cause="findings", findings=4, assets=ap, ref="PR-9")
        self.assertEqual(lines[-1], "action: no escalation (findings below threshold: 4 < 5); continue on codex/gpt-6-2-sol @ medium")
        rc, lines = self.esc("antigravity:gemini-4-argon@high", "antigravity:gemini-4-argon,antigravity:gemini-4-neon",
                             role="orchestrator", cause="findings", findings=2, assets=ap, ref="PR-9")
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> antigravity/gemini-4-neon @ medium", lines[0])
        # validation
        rc, lines = self.esc(cur, avail, cause="findings")
        self.assertEqual(rc, 1, lines)
        self.assertIn("--findings", lines[-1])

    def test_cap_counts_earlier_escalations_per_ref_and_role(self):
        row = {"event": "escalate", "from": "codex:gpt-6-2-sol@low", "to": "codex:gpt-6-2-sol@medium", "cause": "rework"}
        self.seed({**row, "ref": "PR-7", "role": "implementer"}, {**row, "ref": "PR-7", "role": "implementer"},
                  {**row, "ref": "PR-7", "role": "reviewer"}, {**row, "ref": "PR-8", "role": "implementer"},
                  {"event": "select", "ref": "PR-7", "role": "implementer", "provider": "codex", "model": "gpt-6-2-sol", "effort": "low"})
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol", ref="PR 7")
        self.assertEqual((rc, lines), (3, ["action: escalation cap reached (2 of 2) for PR-7: report it to the user with the evidence"]))
        self.assertEqual(len(self.rows()), 5)
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol,antigravity:gemini-4-neon", role="reviewer",
                             implementer="claude-opus-5-5", ref="PR-7")
        self.assertEqual(rc, 0, lines)
        self.assertIn("escalation 2 of 2", lines[0])
        ap = self.prefs("  escalation: {findings: 3, max: 1}\n")
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol", ref="PR-8", assets=ap)
        self.assertEqual(lines[-1], "action: escalation cap reached (1 of 1) for PR-8: report it to the user with the evidence")

    def test_dry_run_logs_nothing(self):
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "codex:gpt-6-2-sol", dry_run=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("note: dry run: nothing logged", lines)
        self.assertTrue(lines[-1].startswith("action: switch the implementer in place to codex/gpt-6-2-sol @ high"))
        self.assertEqual(self.rows(), [])

    def test_argument_validation_and_ignored_cross_role_flags(self):
        avail = "codex:gpt-6-2-sol"
        cases = (
            dict(role="reviewer"),  # no --implementer
            dict(complexity=None),  # implementer without --complexity
            dict(role="orchestrator", coordination=None),
            dict(current="codex:gpt-6-2-sol"),  # no @effort
            dict(available="codex"),
            dict(role="reviewer", implementer="no-such-model"),
        )
        for kw in cases:
            rc, lines = self.esc(kw.pop("current", "codex:gpt-6-2-sol@medium"), kw.pop("available", avail), **kw)
            self.assertEqual(rc, 1, (kw, lines))
            self.assertTrue(lines[-1].startswith("action: fix "), lines)
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", avail, coordination=1, implementer="gpt-6-2-sol", dry_run=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("note: ignored for --role implementer: --coordination, --implementer", lines)

    # ---------- escalate: orchestrator ----------
    def test_orchestrator_climbs_its_same_provider_ladder(self):
        avail = "antigravity:gemini-4-argon,antigravity:gemini-4-neon,claudeAgent:claude-opus-5-5"
        rc, lines = self.esc("antigravity:gemini-4-argon@high", avail, role="orchestrator", cause="status", complexity="hard")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, [
            "note: ignored for --role orchestrator: --complexity",
            "escalate: orchestrator antigravity/gemini-4-argon @ high -> antigravity/gemini-4-neon @ medium (cause status; escalation 1 of 2)",
            "action: switch the orchestrator in place to antigravity/gemini-4-neon @ medium (references/harnesses.md, Switch model in place) "
            "and continue; re-review after the change",
        ])
        self.assertEqual(self.rows()[-1]["role"], "orchestrator")

    def test_orchestrator_provider_switch_asks_and_logs_nothing(self):
        rc, lines = self.esc("antigravity:gemini-4-argon@high", "antigravity:gemini-4-argon,codex:gpt-6-2-sol", role="orchestrator")
        self.assertEqual(rc, 3, lines)
        self.assertEqual(lines[-1], "action: ask the user once before switching the orchestrator's provider to codex/gpt-6-2-sol @ medium "
                                    "(phase boundary only); once approved, rerun this escalate with --approved --to codex:gpt-6-2-sol@medium")
        self.assertEqual(self.rows(), [])
        # the orchestrator's effort cap (high) holds: neon@high is the top of its ladder, opus@high scores lower
        rc2, lines2 = self.esc("antigravity:gemini-4-neon@high", "antigravity:gemini-4-neon,claudeAgent:claude-opus-5-5", role="orchestrator")
        self.assertEqual(rc2, 2, lines2)

    # ---------- log --unflagged ----------
    def test_log_records_unflagged_only_when_given(self):
        args = dict(assets=self.assets, ledger=self.ledger, now="2026-11-20T21:00:00Z", event=None, complexity="hard", risk="high",
                    ui=False, provider="codex", model="gpt-6-2-sol", effort="high", reviewer=None, outcome="ok", findings=4,
                    repo="demo", provisional=False, role="orchestrator", rubric=None, requested=None, effective=None)
        self.assertEqual(_run(orch.log_cmd, _Args(**args, unflagged=3))[0], 0)
        self.assertEqual(_run(orch.log_cmd, _Args(**args))[0], 0)
        rows = self.rows()
        self.assertEqual(rows[0]["unflagged"], 3)
        self.assertNotIn("unflagged", rows[1])

    # ---------- select --role orchestrator --phase ----------
    def phase(self, phase, current, assets=None, avail="antigravity:gemini-4-neon,claudeAgent:claude-opus-5-5", **kw):
        rc, out = _run(orch.select, _sel(assets or self.assets, avail, role="orchestrator", coordination=2, complexity=None,
                                          risk=None, phase=phase, current=current, full=False, **kw))
        return rc, out.rstrip("\n").split("\n")

    def test_phase_effort_off_keeps_the_ladder_output_and_adds_one_note(self):
        rc0, base = self.phase(None, None)
        rc, lines = self.phase("review", "antigravity:gemini-4-neon@medium")
        self.assertEqual((rc0, rc), (0, 0))
        self.assertEqual(lines, base[:-1] + ["note: phase effort is off (preferences.orchestrator.phase_effort); stay on antigravity/gemini-4-neon @ medium", base[-1]])

    def test_phase_effort_on_plan_review_supervise_with_cap_and_floor(self):
        ap = self.prefs("  orchestrator:\n    phase_effort: on\n")
        rc, lines = self.phase("review", "antigravity:gemini-4-neon@medium", assets=ap)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[-2:], ["phase effort: review -> antigravity/gemini-4-neon @ high",
                                      "action: at this phase boundary switch the orchestrator in place to antigravity/gemini-4-neon @ high"])
        self.assertTrue(lines[0].startswith("orchestrator tier T3"), lines)
        rc, lines = self.phase("supervise", "antigravity:gemini-4-neon@medium", assets=ap)
        self.assertEqual(lines[-2:], ["phase effort: supervise -> antigravity/gemini-4-neon @ low",
                                      "action: at this phase boundary switch the orchestrator in place to antigravity/gemini-4-neon @ low"])
        rc, lines = self.phase("plan", "antigravity:gemini-4-neon@medium", assets=ap)
        self.assertEqual(lines[-2:], ["phase effort: plan -> antigravity/gemini-4-neon @ medium", "action: stay on antigravity/gemini-4-neon @ medium"])
        rc, lines = self.phase("review", "antigravity:gemini-4-neon@high", assets=ap)  # xhigh is above max_effort high
        self.assertEqual(lines[-1], "action: stay on antigravity/gemini-4-neon @ high")
        rc, lines = self.phase("supervise", "codex:gpt-6-1-terra@low", assets=ap, avail="codex:gpt-6-2-sol")  # never below low
        self.assertEqual(lines[-1], "action: stay on codex/gpt-6-1-terra @ low")
        capped = self.prefs("  orchestrator:\n    phase_effort: on\n    max_effort: medium\n")
        rc, lines = self.phase("review", "antigravity:gemini-4-neon@medium", assets=capped)
        self.assertEqual(lines[-1], "action: stay on antigravity/gemini-4-neon @ medium")

    def test_phase_flags_validation_and_workers_ignore_them(self):
        rc, lines = self.phase("review", None)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(lines[-1].startswith("action: fix --phase/--current"), lines)
        rc, lines = self.phase("review", "not-a-rung")
        self.assertEqual(rc, 1, lines)
        rc, out = _run(orch.select, _sel(self.assets, "codex:gpt-6-2-sol", complexity="standard", risk="low", phase="review",
                                         current="codex:gpt-6-2-sol@high", full=False))
        self.assertEqual(rc, 0, out)
        self.assertIn("note: ignored for --role implementer: --phase, --current", out)

    # ---------- history --by ----------
    def by(self, role="implementer", by="model,effort", full=False, **kw):
        rc, out = _run(orch.history_cmd, _Args(ledger=self.ledger, assets=self.assets, ref=None, last=None, now="2026-11-20T21:00:00Z",
                                               full=full, role=role, by=by, **kw))
        return rc, out.rstrip("\n").split("\n")

    def test_history_by_model_effort_aggregates_attempts(self):
        sol = {"provider": "codex", "model": "gpt-6-2-sol"}
        self.seed(
            {"event": "select", "ref": "PR-1", **sol, "effort": "medium"},
            {"event": "complete", "ref": "PR-1", **sol, "effort": "medium", "outcome": "rework", "findings": 4},
            {"event": "escalate", "ref": "PR-1", "role": "implementer", "from": "codex:gpt-6-2-sol@medium", "to": "codex:gpt-6-2-sol@high", "cause": "rework"},
            {"event": "select", "ref": "PR-1", **sol, "effort": "high"},
            {"event": "complete", "ref": "PR-1", **sol, "effort": "high", "outcome": "ok", "findings": 1},
            {"event": "select", "ref": "PR-2", **sol, "effort": "medium", "role": "implementer"},
            {"event": "complete", "ref": "PR-2", **sol, "effort": "medium", "outcome": "ok", "findings": 0},
            {"event": "select", "ref": "PR-3", "provider": "claude", "model": "Claude Opus 5.5", "effort": "high"},
            {"event": "complete", "ref": "PR-3", "provider": "claude", "model": "claude-opus-5-5", "effort": "high", "outcome": "ok", "findings": 2},
            {"event": "select", "ref": "PR-1", "role": "reviewer", "provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
            {"event": "select", "ref": "PR-1", "role": "orchestrator", "provider": "antigravity", "model": "gemini-4-neon", "effort": "medium"},
            {"event": "complete", "ref": "PR-1", "role": "orchestrator", "provider": "antigravity", "model": "gemini-4-neon", "effort": "medium",
             "outcome": "ok", "findings": 3, "unflagged": 1},
            {"event": "complete", "ref": "PR-4", "provider": "mystery", "model": "m1", "effort": "low", "outcome": "rework"},
        )
        rc, lines = self.by()
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, [
            "codex:gpt-6-2-sol@medium: 2 item(s); rework 50%; findings avg 2.0; escalations 1; cost normal",
            "claude:claude-opus-5-5@high: 1 item(s); rework 0%; findings avg 2.0; escalations 0; cost expensive",
            "codex:gpt-6-2-sol@high: 1 item(s); rework 0%; findings avg 1.0; escalations 0; cost normal",
            "mystery:m1@low: 1 item(s); rework 100%; findings avg n/a; escalations 0; cost ?",
            "ok: 4 combination(s) for role implementer",
        ])
        rc, lines = self.by(role="orchestrator")
        self.assertEqual(lines, [
            "antigravity:gemini-4-neon@medium: 1 item(s); rework 0%; findings avg 3.0; unflagged avg 1.0; escalations 0; cost normal",
            "ok: 1 combination(s) for role orchestrator",
        ])

    def test_history_by_caps_and_validates(self):
        self.seed(*({"event": "select", "ref": "item-%02d" % i, "provider": "claude", "model": "m%02d" % i, "effort": "high"} for i in range(15)))
        rc, lines = self.by()
        self.assertEqual(rc, 0, lines)
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[-2:], ["… 5 more (rerun with --full)", "ok: 15 combination(s) for role implementer"])
        self.assertEqual(len(self.by(full=True)[1]), 16)
        rc, lines = self.by(role=None)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(lines[-1].startswith("error: 1 problem(s); action: rerun history with --role"), lines)
        rc, lines = self.by(by="model")
        self.assertEqual(rc, 1, lines)

    # ---------- fix round: unscored rungs, reviewer independence, --approved ----------
    def test_unscored_current_rung_never_steps_down(self):
        self.assertIsNone(orch._rung_score({"scores": {"high": 51}}, "medium"))
        repros = (  # (current, available, extra args) that used to "escalate" onto a weaker model
            ("codex:gpt-6-2-astra@xhigh", "codex:gpt-6-2-astra,codex:gpt-6-luna", dict(complexity="trivial")),
            ("claude:claude-fable-5-2@high", "claude:claude-fable-5-2,claude:claude-sonnet-5-5", {}),
            ("antigravity:gemini-pro-orchestrator-neon@high", "antigravity:gemini-pro-orchestrator-neon,antigravity:gemini-4-hydrogen",
             dict(role="orchestrator", coordination=0)),
        )
        for current, avail, kw in repros:
            rc, lines = self.esc(current, avail, **kw)
            self.assertEqual(rc, 3, (current, lines))
            here = current.replace(":", "/", 1).replace("@", " @ ")
            self.assertEqual(lines[-1], f"action: ask the user once (current rung {here} has no rated score, so no higher rung can be proven); "
                                        "once approved, rerun this escalate with --approved --to <P:M@E>")
        self.assertEqual(self.rows(), [])
        # an unrated effort may still climb to a higher RATED effort of the same model (argon rates only high)
        rc, lines = self.esc("antigravity:gemini-4-argon@medium", "antigravity:gemini-4-argon,antigravity:gemini-4-hydrogen")
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> antigravity/gemini-4-argon @ high (cause rework", lines[0])

    def test_reviewer_escalation_follows_the_reviewer_fallback_order(self):
        # (a) a reviewer on the implementer's own model skips the same-model effort step
        rc, lines = self.esc("claude:claude-opus-5-5@high", "claude:claude-opus-5-5,codex:gpt-6-2-sol", role="reviewer",
                             implementer="claude-opus-5-5")
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> codex/gpt-6-2-sol @ medium (cause rework", lines[0])
        # (b) a different family beats the same provider and family as the implementer
        rc, lines = self.esc("claude:claude-sonnet-5-5@xhigh", "claude:claude-opus-5-5,claude:claude-sonnet-5-5,codex:gpt-6-2-sol",
                             role="reviewer", implementer="claude-haiku-5-5", ref="PR-8")
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> codex/gpt-6-2-sol @ medium (cause rework", lines[0])

    def test_approved_rerun_logs_the_asked_switch(self):
        avail = "antigravity:gemini-4-argon,codex:gpt-6-2-sol"
        rc, lines = self.esc("antigravity:gemini-4-argon@high", avail, role="orchestrator", cause="status", approved=True, to="codex:gpt-6-2-sol@medium")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, [
            "escalate: orchestrator antigravity/gemini-4-argon @ high -> codex/gpt-6-2-sol @ medium (cause status; escalation 1 of 2; approved)",
            "action: at this phase boundary switch the orchestrator in place to codex/gpt-6-2-sol @ medium "
            "(references/harnesses.md, Switch model in place) and continue; re-review after the change",
        ])
        self.assertEqual({k: v for k, v in self.rows()[-1].items() if k not in ("ts", "repo")}, {
            "event": "escalate", "role": "orchestrator", "ref": "PR-7", "from": "antigravity:gemini-4-argon@high",
            "to": "codex:gpt-6-2-sol@medium", "cause": "status", "approved": True})
        # unknown current score: the user's approval stands in for the proof
        rc, lines = self.esc("claude:claude-fable-5-2@high", "claude:claude-fable-5-2", approved=True, to="claude:claude-opus-5-5@xhigh", ref="PR-8")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.rows()[-1]["to"], "claude:claude-opus-5-5@xhigh")
        n = len(self.rows())
        for kw in (dict(approved=True), dict(to="codex:gpt-6-2-sol@high"),  # the two go together
                   dict(approved=True, to="codex:gpt-6-2-sol@high", current="claude:claude-opus-5-5@xhigh"),  # 54.89 < 57.44: a step down
                   dict(approved=True, to="claude:claude-opus-5-5@high", role="reviewer", implementer="claude-opus-5-5"),  # independence
                   dict(approved=True, to="claude:claude-opus-5-5@none", current="claude:claude-opus-5-5@high"),  # unrated lower effort
                   dict(approved=True, to="claude:claude-fable-5-2@low", current="claude:claude-fable-5-2@xhigh"),  # unscored, lower
                   dict(approved=True, to="claude:claude-fable-5-2@high", current="claude:claude-fable-5-2@high"),  # same rung
                   dict(approved=True, to="nowhere:claude-opus-5-5@xhigh")):  # provider not offered
            rc, lines = self.esc(kw.pop("current", "codex:gpt-6-2-sol@medium"), "codex:gpt-6-2-sol,claude:claude-opus-5-5", ref="PR-9", **kw)
            self.assertEqual(rc, 1, (kw, lines))
            self.assertTrue(lines[-1].startswith("action: fix "), lines)
        self.assertEqual(len(self.rows()), n)
        ap = self.prefs("  escalation: {findings: 3, max: 1}\n")  # the cap still applies
        rc, lines = self.esc("claude:claude-fable-5-2@high", "claude:claude-fable-5-2", approved=True, to="claude:claude-opus-5-5@xhigh",
                             ref="PR-8", assets=ap)
        self.assertEqual(lines[-1], "action: escalation cap reached (1 of 1) for PR-8: report it to the user with the evidence")

    def test_current_pair_added_by_catalog_id(self):
        rc, lines = self.esc("antigravity:Orchestrator (Argon)@high", "antigravity:gemini-4-hydrogen", role="orchestrator", coordination=0)
        self.assertNotIn("not in catalog", "\n".join(lines))

    def test_current_pair_missing_from_available_is_added(self):
        rc, lines = self.esc("codex:gpt-6-2-sol@medium", "claude:claude-opus-5-5")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], "note: --current pair added to --available")
        self.assertIn("-> codex/gpt-6-2-sol @ high", lines[1])

    def test_launch_header_names_escalate_and_its_configured_thresholds(self):
        cand = {"provider": "codex", "pair": "codex:gpt-6-2-sol", "model": {"id": "gpt-6-2-sol"}, "effort": "medium"}
        ws = {"present": False}
        line = next(l for l in orch._build_launch_header("orchestrator", cand, [cand], None, 2, {}, ws, "r", "hard", "low", thresholds=(5, 4))
                    if l.startswith("escalate on"))
        self.assertIn("| 4+ review findings your brief did not flag (workers: 5+) |", line)

    def test_history_by_rework_rate_effortless_rows_and_catalog(self):
        cat = os.path.join(self.dir, "cat.yaml")
        _write(cat, "index_versions: {aa: 1}\nemergency_efforts: [max]\nmodels:\n"
                    "  - {id: m1, name: M1, family: x, cost: cheap, rating: rated, source: aa, scores: {low: 30}}\n")
        self.seed(
            {"event": "complete", "ref": "PR-1", "provider": "p", "model": "m1", "effort": "low", "outcome": "rework"},
            {"event": "select", "ref": "PR-2", "provider": "p", "model": "m1", "effort": "low"},
            {"event": "complete", "ref": "PR-2", "provider": "p", "model": "m1", "effort": "low", "outcome": "failed"},
            {"event": "complete", "ref": "PR-3", "provider": "p", "model": "m1", "effort": "low", "outcome": "ok"},
            {"event": "select", "ref": "PR-4", "provider": "p", "model": "m1"},
        )
        rc, lines = self.by(catalog=cat)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, [
            "p:m1@low: 3 item(s); rework 50%; findings avg n/a; escalations 0; cost cheap",
            "p:m1@?: 1 item(s); rework n/a; findings avg n/a; escalations 0; cost cheap",
            "ok: 2 combination(s) for role implementer",
        ])

    # ---------- check ----------
    def test_check_validates_escalation_and_phase_preferences(self):
        bad = self.prefs("  escalation: {findings: 0, max: 4}\n  orchestrator:\n    unflagged_findings: x\n    phase_effort: maybe\n")
        rc, out = _run(orch.check, _Args(assets=bad, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="", full=True))
        self.assertEqual(rc, 1, out)
        for key in ("escalation.findings", "escalation.max", "orchestrator.unflagged_findings", "orchestrator.phase_effort"):
            self.assertIn(key, out)
        good = self.prefs("  escalation: {findings: 3, max: 0}\n  orchestrator:\n    unflagged_findings: 3\n    phase_effort: off\n")
        rc, out = _run(orch.check, _Args(assets=good, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="", full=True))
        self.assertEqual(rc, 0, out)
        notmap = self.prefs("  escalation: 3\n")
        rc, out = _run(orch.check, _Args(assets=notmap, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", lockfile="", full=True))
        self.assertEqual(rc, 1, out)


class TestLedgerProviderIdsDuplicatesAndAvailableFile(unittest.TestCase):
    """Verbatim provider ids in the ledger, the duplicate-`complete` warning, and `--available @file`."""

    LOG = dict(now="2026-11-20T21:00:00Z", event="select", complexity="hard", risk="low", ui=False, surface="subagent",
               provider="claudeAgent_claude_extra", model="claude-opus-5-5", effort="high", reviewer=None, outcome=None,
               findings=None, repo="demo", provisional=False, ref=None, failure_class=None, role=None, rubric=None,
               requested=None, effective=None)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.assets = os.path.join(self.dir, "assets.yaml")
        self.ledger = os.path.join(self.dir, "ledger.jsonl")
        _write(self.assets, "version: 1\nproviders:\n  - id: claudeAgent_claude_extra\n    status: ok\n  - id: codex\n    status: ok\n")

    def log(self, **kw):
        return _run(orch.log_cmd, _Args(**{**self.LOG, "assets": self.assets, "ledger": self.ledger, "catalog": CATALOG_PATH, **kw}))

    def rows(self):
        with open(self.ledger, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def cli(self, *args):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "orch.py"), *args, "--assets", self.assets, "--catalog", CATALOG_PATH,
                               "--now", "2026-11-20T21:00:00Z"], capture_output=True, text=True, cwd=self.dir)
        return proc.returncode, proc.stdout

    def avail_file(self, text, name="avail.txt"):
        path = os.path.join(self.dir, name)
        _write(path, text)
        return "@" + path

    # ---------- verbatim provider ids ----------
    def test_requested_effective_keep_the_provider_id_verbatim(self):
        rc, out = self.log(requested="  claudeAgent_claude_extra:Claude Opus 5.5@HIGH ", effective="claudeAgent_claude_extra/claude-opus-5-5 @ high")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines(), [f"logged select (demo) -> {self.ledger}"])
        row = self.rows()[0]
        self.assertEqual(row["requested"], "claudeAgent_claude_extra:claude-opus-5-5@high")
        self.assertEqual(row["effective"], "claudeAgent_claude_extra:claude-opus-5-5@high")
        self.assertNotIn("mismatch", row)

    def test_mismatch_compares_normalised_providers(self):
        rc, out = self.log(requested="claudeAgent_claude_extra:claude-opus-5-5@high", effective="claudeagent-claude-extra:claude-opus-5-5@high")
        self.assertEqual((rc, "warning:" in out), (0, False), out)
        rc, out = self.log(requested="claudeAgent_claude_extra:claude-opus-5-5@high", effective="claudeAgent:claude-opus-5-5@high")
        self.assertIn("warning: effective claudeAgent:claude-opus-5-5@high differs from requested claudeAgent_claude_extra:claude-opus-5-5@high", out)
        self.assertTrue(self.rows()[-1]["mismatch"])

    def test_escalate_rows_keep_the_provider_id_verbatim(self):
        rc, out = _run(orch.escalate_cmd, _Args(
            assets=self.assets, catalog=CATALOG_PATH, now="2026-11-20T21:00:00Z", ledger=self.ledger, role="implementer", ref="PR-7",
            current="Codex:gpt-6-2-sol@medium", cause="rework", available="Codex:gpt-6-2-sol", findings=None, complexity="standard", risk="low",
            coordination=None, implementer=None, dry_run=False, repo="demo", full=False, approved=False, to=None))
        self.assertEqual(rc, 0, out)
        row = self.rows()[0]
        self.assertEqual((row["from"], row["to"]), ("Codex:gpt-6-2-sol@medium", "Codex:gpt-6-2-sol@high"))

    def test_history_by_groups_old_lowercase_and_new_verbatim_rows(self):
        sol = {"model": "gpt-6-2-sol", "effort": "medium"}
        with open(self.ledger, "w", encoding="utf-8") as fh:
            for e in ({"event": "select", "ref": "A", "provider": "claudeagent-claude-extra", "model": "claude-opus-5-5", "effort": "high"},
                      {"event": "complete", "ref": "A", "provider": "claudeagent-claude-extra", "model": "claude-opus-5-5", "effort": "high", "outcome": "ok", "findings": 1},
                      {"event": "select", "ref": "B", "provider": "claudeAgent_claude_extra", "model": "claude-opus-5-5", "effort": "high"},
                      {"event": "complete", "ref": "B", "provider": "claudeAgent_claude_extra", "model": "claude-opus-5-5", "effort": "high", "outcome": "rework", "findings": 3},
                      {"event": "escalate", "ref": "B", "role": "implementer", "from": "claudeAgent_claude_extra:claude-opus-5-5@high",
                       "to": "claudeAgent_claude_extra:claude-opus-5-5@xhigh", "cause": "rework"},
                      {"event": "select", "ref": "C", "provider": "codex", **sol}):
                fh.write(json.dumps(e) + "\n")
        rc, out = _run(orch.history_cmd, _Args(ledger=self.ledger, assets=self.assets, catalog=CATALOG_PATH, ref=None, last=None,
                                               full=True, role="implementer", by="model,effort"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), [
            "claudeAgent_claude_extra:claude-opus-5-5@high: 2 item(s); rework 50%; findings avg 2.0; escalations 1; cost expensive",
            "codex:gpt-6-2-sol@medium: 1 item(s); rework n/a; findings avg n/a; escalations 0; cost normal",
            "ok: 2 combination(s) for role implementer",
        ])

    # ---------- duplicate complete ----------
    def test_second_complete_for_an_attempt_warns_but_logs(self):
        self.log(ref="PR-9")
        self.assertNotIn("warning:", self.log(ref="PR-9", event="complete", outcome="rework")[1])
        rc, out = self.log(ref="PR-9", event="complete", outcome="ok")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), [
            "warning: PR-9 (implementer) already has a complete for this attempt (outcome rework); history keeps the latest — "
            "log one complete per attempt, after the review gate",
            f"logged complete (demo) -> {self.ledger}",
        ])
        self.assertEqual(len(self.rows()), 3)
        # a role has its own attempt; a new select starts a new attempt; no --ref means nothing to match
        self.assertNotIn("warning:", self.log(ref="PR-9", event="complete", outcome="ok", role="reviewer")[1])
        self.log(ref="PR-9")
        self.assertNotIn("warning:", self.log(ref="PR-9", event="complete", outcome="ok")[1])
        self.assertNotIn("warning:", self.log(event="complete", outcome="ok")[1])
        self.assertNotIn("warning:", self.log(event="complete", outcome="ok")[1])

    # ---------- --available @file ----------
    def test_available_file_for_select_escalate_and_launch(self):
        avail = self.avail_file("# discovery\ncodex:gpt-6-2-sol, claude:claude-opus-5-5  # inline note\n\n\nantigravity:gemini-4-neon\n")
        rc, out = self.cli("select", "--complexity", "standard", "--risk", "low", "--available", avail)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], self.cli("select", "--complexity", "standard", "--risk", "low", "--available",
                                                                "codex:gpt-6-2-sol,claude:claude-opus-5-5,antigravity:gemini-4-neon")[1].strip().splitlines()[-1])
        rc, out = self.cli("escalate", "--role", "implementer", "--ref", "PR-1", "--current", "codex:gpt-6-2-sol@medium", "--cause", "rework",
                           "--complexity", "standard", "--risk", "low", "--available", avail, "--ledger", self.ledger)
        self.assertEqual(rc, 0, out)
        self.assertIn("escalate: implementer codex/gpt-6-2-sol @ medium -> ", out)
        _write(os.path.join(self.dir, "brief.md"), "GOAL: ship it.\n")
        _write(os.path.join(self.dir, "t.launch.yaml"),
               "version: 1\nrole: orchestrator\ntitle: t\nrepo: o/r\nbrief: brief.md\n"
               "rubric: {size: 2, uncertainty: 1, deps: 2, consequences: 1, verification: 1, coordination: 2}\nladder: auto\n"
               "workspace: {strategy: worktree, base: origin/main, children: shared}\n")
        rc, out = self.cli("launch", os.path.join(self.dir, "t.launch.yaml"), "--available", avail, "--no-probe")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("available:", out)

    def test_unreadable_available_file_names_the_path(self):
        missing = os.path.join(self.dir, "nope.txt")
        for cmd in (("select", "--complexity", "standard", "--risk", "low"), ("check",)):
            rc, out = self.cli(*cmd, "--available", "@" + missing)
            self.assertEqual(rc, 1, out)
            lines = out.strip().splitlines()
            self.assertEqual(lines[0], f"error: --available file {missing} is not readable")
            self.assertTrue(lines[-1].startswith("error: 1 problem(s); action: fix --available "), lines)
            self.assertTrue(lines[-1].endswith(f"and rerun {cmd[0]}"), lines)


if __name__ == "__main__":
    unittest.main()
