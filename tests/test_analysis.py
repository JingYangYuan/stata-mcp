import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import stata_mcp_analysis as analysis  # noqa: E402


class HelpEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stata_root = Path(self.tmp.name) / "Stata"
        base_r = self.stata_root / "ado" / "base" / "r"
        base_r.mkdir(parents=True)
        (base_r / "rhelp_alias.maint").write_text("reg\t\tregress\nre\t\tregress\n", encoding="utf-8")
        (base_r / "regress.sthlp").write_text(
            "{smcl}\n"
            "{* *! version 1.6.1}\n"
            "{vieweralsosee \"[R] anova\" \"help anova\"}\n"
            "{p2col:{bf:[R] regress} {hline 2}}Linear regression{p_end}\n"
            "{marker syntax}{...}\n"
            "{title:Syntax}\n"
            "{p 8 16 2}{opt regress} {depvar} [{indepvars}] {ifin}, [{it:options}]{p_end}\n"
            "{synopt :{opt nocons:tant}}suppress constant term{p_end}\n"
            "{synopt :{opth vce(regress##vcetype:vcetype)}}variance estimator{p_end}\n"
            "{marker options}{...}\n"
            "{title:Options}\n"
            "{phang}{opt l:evel(#)} set confidence level.{p_end}\n"
            "{INCLUDE opt_demo}\n"
            "{marker results}{...}\n"
            "{title:Stored results}\n"
            "{pstd}Stores e(N) and e(r2).{p_end}\n",
            encoding="utf-8",
        )
        base_o = self.stata_root / "ado" / "base" / "o"
        base_o.mkdir(parents=True)
        (base_o / "opt_demo.ihlp").write_text("{phang}{opt b:eta} standardized beta.{p_end}\n", encoding="utf-8")
        (base_r / "regress.ado").write_text("*! version 1.2.3  01jan2025\nprogram define regress\nend\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_alias_and_smcl_rendering(self):
        res = analysis.resolve_help("reg", stata_path=str(self.stata_root), section="syntax")
        self.assertTrue(res["found"])
        self.assertEqual(res["resolved_topic"], "regress")
        self.assertIn("nocons[tant]", res["content"])
        self.assertIn("vce(vcetype)", res["content"])
        self.assertNotIn("Stored results", res["content"])

    def test_include_and_options_section(self):
        res = analysis.resolve_help("regress", stata_path=str(self.stata_root), section="options")
        self.assertTrue(res["found"])
        self.assertIn("l[evel](#)", res["content"])
        self.assertIn("b[eta]", res["content"])

    def test_search_and_which_local(self):
        sres = analysis.search_help("reg", stata_path=str(self.stata_root))
        self.assertGreaterEqual(sres["count"], 2)
        self.assertEqual(sres["results"][0]["target"], "regress")

        wres = analysis.which_local("regress", stata_path=str(self.stata_root))
        self.assertTrue(wres["found"])
        self.assertIn("1.2.3", wres["version_header"])


class ErrorLocatorTests(unittest.TestCase):
    def test_parse_statements_delimit_comments_and_continuations(self):
        src = (
            "* header comment\n"
            "sysuse auto, clear // inline comment\n"
            "/* block\n"
            "   comment */\n"
            "regress price ///\n"
            "    mpg weight\n"
            "#delimit ;\n"
            "display\n"
            "  1 + 2;\n"
            "#delimit cr\n"
            "display 99\n"
        )
        stmts = analysis.parse_do_statements(src)
        self.assertEqual(len(stmts), 4)
        self.assertEqual(stmts[1]["start_line"], 5)
        self.assertEqual(stmts[1]["end_line"], 6)
        self.assertEqual(stmts[1]["verb"], "regress")
        self.assertEqual(stmts[2]["verb"], "display")
        self.assertEqual(stmts[3]["start_line"], 11)

    def test_monotonic_locator_handles_duplicate_commands(self):
        src = (
            "sysuse auto, clear\n"
            "display \"ok\"\n"
            "regress price mpg\n"
            "display \"ok\"\n"
            "regress price bad_var\n"
            "display \"never reached\"\n"
        )
        records = [
            {"seq": 1, "step": 1, "kind": "command", "text": ". sysuse auto, clear"},
            {"seq": 2, "step": 2, "kind": "command", "text": '. display "ok"'},
            {"seq": 3, "step": 3, "kind": "command", "text": ". regress price mpg"},
            {"seq": 4, "step": 4, "kind": "command", "text": '. display "ok"'},
            {"seq": 5, "step": 5, "kind": "command", "text": ". regress price bad_var"},
            {"seq": 6, "step": 5, "kind": "output", "text": "variable bad_var not found"},
            {"seq": 7, "step": 5, "kind": "error", "text": "r(111);"},
            {"seq": 8, "step": 5, "kind": "output", "text": "end of do-file"},
        ]
        diag = analysis.locate_error_in_do(src, records, file_path="/tmp/test.do")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["line"], 5)
        self.assertEqual(diag["error_code"], "r(111)")
        self.assertIn(">>>    5 | regress price bad_var", diag["source_context"])

    def test_macro_expanded_command_and_line_offset_inside_loop(self):
        src = (
            "sysuse auto, clear\n"
            "local target nonexistent_var\n"
            "foreach v in mpg weight {\n"
            "    summarize `v'\n"
            "    regress price `target'\n"
            "}\n"
        )
        records = [
            {"seq": 1, "step": 1, "kind": "command", "text": ". sysuse auto, clear"},
            {"seq": 2, "step": 2, "kind": "command", "text": ". local target nonexistent_var"},
            {"seq": 3, "step": 3, "kind": "command", "text": ". foreach v in mpg weight {"},
            {"seq": 4, "step": 3, "kind": "output", "text": "  2.     summarize mpg"},
            {"seq": 5, "step": 3, "kind": "output", "text": "  3.     regress price nonexistent_var"},
            {"seq": 6, "step": 3, "kind": "output", "text": "variable nonexistent_var not found"},
            {"seq": 7, "step": 3, "kind": "error", "text": "r(111);"},
            {"seq": 8, "step": 3, "kind": "output", "text": "end of do-file"},
        ]
        diag = analysis.locate_error_in_do(src, records, file_path="/tmp/loop.do", line_offset=10)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["line"], 15)
        self.assertEqual(diag["confidence"], "high")

    def test_capture_error_is_not_treated_as_fatal(self):
        records = [
            {"seq": 1, "step": 1, "kind": "command", "text": ". capture drop foo"},
            {"seq": 2, "step": 1, "kind": "error", "text": "r(111);"},
            {"seq": 3, "step": 2, "kind": "command", "text": ". display 1+1"},
            {"seq": 4, "step": 2, "kind": "output", "text": "2"},
            {"seq": 5, "step": 2, "kind": "marker", "text": "*** Execution completed ***"},
        ]
        self.assertIsNone(analysis.detect_stata_error(records))


class AntiLoopLinterTests(unittest.TestCase):
    def test_detects_major_anti_loop_patterns_and_traps(self):
        bad_code = (
            "forvalues i = 1/`=_N' {\n"
            "    replace z = x[`i'] in `i'\n"
            "}\n"
            "levelsof ind, local(inds)\n"
            "foreach g of local inds {\n"
            "    preserve\n"
            "    summarize y if ind == `g'\n"
            "    restore\n"
            "}\n"
            "foreach v of varlist x1 x2 {\n"
            "    replace tot = tot + `v'\n"
            "}\n"
            "keep if price > 0\n"
        )
        res = analysis.lint_do_code(bad_code)
        rule_ids = {i["rule_id"] for i in res["issues"]}
        self.assertIn("ANTI_LOOP_OBS_INDEX", rule_ids)
        self.assertIn("ANTI_LOOP_LEVELSOF", rule_ids)
        self.assertIn("ANTI_LOOP_PRESERVE_RESTORE", rule_ids)
        self.assertIn("ANTI_LOOP_ROW_ACCUM", rule_ids)
        self.assertIn("PITFALL_MISSING_GT", rule_ids)
        self.assertLess(res["idiomatic_score"], 50)

    def test_clean_regression_spec_loop_is_not_flagged(self):
        good_code = (
            "sysuse auto, clear\n"
            "foreach y in price mpg {\n"
            "    regress `y' weight foreign\n"
            "}\n"
            "keep if price > 5000 & !missing(price)\n"
        )
        res = analysis.lint_do_code(good_code)
        self.assertEqual(res["anti_loop_count"], 0)
        self.assertEqual(res["idiomatic_score"], 100)

    def test_vectorize_guide_from_code_and_chinese_keyword(self):
        res1 = analysis.vectorize_guide(
            code="levelsof g, local(gs)\nforeach k of local gs {\n    egen m = mean(x) if g == `k'\n}\n"
        )
        ids1 = {r["id"] for r in res1["recipes"]}
        self.assertIn("group_agg", ids1)

        res2 = analysis.vectorize_guide(pattern="滞后")
        ids2 = {r["id"] for r in res2["recipes"]}
        self.assertIn("lag_lead_diff", ids2)


if __name__ == "__main__":
    unittest.main()
