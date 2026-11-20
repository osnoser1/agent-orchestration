import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "scripts")
CATALOG_PATH = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "catalog.yaml")
FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures", "agy-usage-sample.json")

sys.path.insert(0, SCRIPTS_DIR)
import orch  # noqa: E402


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
            ["gemini-4-neon", "gemini-4-hydrogen", "Orchestrator (Neon)", "gemini-3-8-flash"],
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

        # Unmatched model names kept as-is and reported once.
        self.assertEqual(result["unmatched"], ["Orchestrator (Neon)"])

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
            args = mock.Mock(
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
            self.assertIn("Orchestrator (Neon)", out)

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
            self.assertIn("Orchestrator (Neon)", by_name["Gemini Models"]["models"])

            check_args = mock.Mock(assets=assets_path, catalog=CATALOG_PATH)
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
            probe_args = mock.Mock(
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
            sel_args = mock.Mock(
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
            sel_ui = mock.Mock(**{**sel_args.__dict__, "ui": True, "available": "antigravity:claude-opus-5-5,antigravity:gemini-4-neon"})
            buf_ui = io.StringIO()
            with contextlib.redirect_stdout(buf_ui):
                rc_ui = orch.select(sel_ui)
            self.assertEqual(rc_ui, 0)
            self.assertIn("implementer: antigravity / gemini-4-neon @ high", buf_ui.getvalue())

            # After the Claude/GPT group reset passes, antigravity:claude-opus-5-5 becomes eligible again.
            sel_after = mock.Mock(
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
            sel_args = mock.Mock(
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
            sel_blocked = mock.Mock(**{**sel_args.__dict__, "available": "antigravity:gemini-4-neon", "fail_on_low": True})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orch.select(sel_blocked), 3)

            sel_ok = mock.Mock(**{**sel_args.__dict__, "available": "antigravity:gemini-4-argon", "fail_on_low": True})
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
            sel_args = mock.Mock(
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
            probe_args = mock.Mock(
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
            sel_args = mock.Mock(
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


if __name__ == "__main__":
    unittest.main()
