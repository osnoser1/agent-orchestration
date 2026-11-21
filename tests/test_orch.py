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
            self.assertIn("reviewer (T4, same model, fresh conversation, below tier): antigravity / gemini-4-argon @ high", out)

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
            "action: fix the --implementer id (nope is not in the catalog) and rerun select",
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


def _hist(ledger, assets=None, ref=None, last=None, full=True):
    return _Args(ledger=ledger, assets=assets, ref=ref, last=last, now="2026-11-20T21:00:00Z", full=full)


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


if __name__ == "__main__":
    unittest.main()
