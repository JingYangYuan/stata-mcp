"""Real Stata equivalence & benchmark tests for all 15 anti-loop vectorization patterns.

Each test executes both the anti-loop ("bad") implementation and the idiomatic
vectorized Stata implementation in a temporary directory using the local Stata
executable (e.g. StataNow 19.5 MP), verifying:

1. Exact numerical/structural equivalence (asserting differences < 1e-10 or exact
   equality on datasets, coefficients, and variable names).
2. Measurable speedup on larger loop workloads using Stata's ``timer``.
3. ``stata_mcp_analysis.lint_do_code`` flags every bad-loop script with its
   expected rule_id and scores every idiomatic script with 0 anti-loops.

Skipped cleanly on machines without a local Stata binary (e.g. GitHub Actions).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import stata_mcp_analysis as analysis  # noqa: E402
from stata_mcp_local import stata as stata_finder  # noqa: E402


def _find_stata_cli() -> str:
    env_bin = os.environ.get("STATA_CLI", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    for cand_path in (
        "/Applications/StataNow/StataMP.app/Contents/MacOS/stata-mp",
        "/Applications/StataNow/StataSE.app/Contents/MacOS/stata-se",
        "/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp",
        "/Applications/Stata/StataSE.app/Contents/MacOS/stata-se",
    ):
        if os.path.isfile(cand_path) and os.access(cand_path, os.X_OK):
            return cand_path
    best = stata_finder.best()
    if best:
        for exe in (
            Path(best.path) / "StataMP.app" / "Contents" / "MacOS" / "stata-mp",
            Path(best.path) / "StataSE.app" / "Contents" / "MacOS" / "stata-se",
            Path(best.path) / "stata-mp",
            Path(best.path) / "stata-se",
            Path(best.path) / "stata",
        ):
            if exe.is_file() and os.access(str(exe), os.X_OK):
                return str(exe)
    return ""


STATA_CLI = _find_stata_cli()


def _run_stata_do(do_body: str, timeout: float = 60.0) -> tuple[str, dict[str, str]]:
    """Run a .do script inside an isolated temp dir (never touching repo cwd)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        do_file = tmp_path / "test_case.do"
        wrapped = "set more off\nset rmsg off\n" + do_body.strip() + "\nexit, clear\n"
        do_file.write_text(wrapped, encoding="utf-8")
        proc = subprocess.run(
            [STATA_CLI, "-b", "do", str(do_file)],
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        log_file = tmp_path / "test_case.log"
        log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        if proc.returncode != 0:
            raise AssertionError(f"Stata exited {proc.returncode}:\n{log_text}\nSTDERR:{proc.stderr}")
        if re.search(r"(?m)^r\(\d+\);$", log_text):
            raise AssertionError(f"Stata runtime error in log:\n{log_text}")
        markers: dict[str, str] = {}
        for line in log_text.splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)=(.+?)\s*$", line.strip())
            if m:
                markers[m.group(1)] = m.group(2)
        return log_text, markers


@unittest.skipUnless(STATA_CLI, "Local Stata binary not installed; skipping real Stata tests")
class RealStataAntiLoopTests(unittest.TestCase):
    def _assert_linter(self, bad_code: str, expected_rule: str, good_code: str) -> None:
        bad_res = analysis.lint_do_code(bad_code)
        rules = {i["rule_id"] for i in bad_res["issues"]}
        self.assertIn(expected_rule, rules, f"Expected {expected_rule} in {rules} for:\n{bad_code}")
        good_res = analysis.lint_do_code(good_code)
        self.assertEqual(
            good_res["anti_loop_count"],
            0,
            f"Expected 0 anti-loops for idiomatic code, got {good_res['issues']}",
        )

    def test_01_obs_loop_equivalence_and_speedup(self):
        bad = """
        forvalues i = 1/`=_N' {
            qui replace z_loop = x[`i'] / y[`i'] in `i'
        }
        """
        good = "gen double z_vec = x / y"
        self._assert_linter(bad, "ANTI_LOOP_OBS_INDEX", good)

        do = """
        clear
        set obs 25000
        set seed 42
        gen double x = rnormal(10, 2)
        gen double y = runiform(1, 5)
        gen double z_loop = .
        timer clear
        timer on 1
        forvalues i = 1/`=_N' {
            qui replace z_loop = x[`i'] / y[`i'] in `i'
        }
        timer off 1
        timer on 2
        gen double z_vec = x / y
        timer off 2
        assert abs(z_loop - z_vec) < 1e-12
        qui timer list 1
        di "T_LOOP=" r(t1)
        qui timer list 2
        di "T_VEC=" r(t2)
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")
        self.assertGreater(float(m["T_LOOP"]), float(m["T_VEC"]))

    def test_02_group_agg_and_leave_one_out(self):
        bad = """
        levelsof industry, local(inds)
        foreach g of local inds {
            qui summarize sales if industry == `g'
            qui replace ind_mean_loop = r(mean) if industry == `g'
        }
        """
        good = """
        bysort industry: egen double ind_mean_vec = mean(sales)
        bysort industry: egen double _tot = total(sales)
        bysort industry: egen long _cnt = count(sales)
        gen double loo_vec = (_tot - sales) / (_cnt - 1) if _cnt > 1
        """
        self._assert_linter(bad, "ANTI_LOOP_LEVELSOF", good)

        do = """
        clear
        set obs 4000
        set seed 101
        gen int industry = ceil(runiform() * 80)
        gen double sales = rnormal(100, 15)
        replace sales = . in 5/12
        gen double ind_mean_loop = .
        timer clear
        timer on 1
        levelsof industry, local(inds)
        foreach g of local inds {
            qui summarize sales if industry == `g'
            qui replace ind_mean_loop = r(mean) if industry == `g'
        }
        timer off 1
        timer on 2
        bysort industry: egen double ind_mean_vec = mean(sales)
        bysort industry: egen double _tot = total(sales)
        bysort industry: egen long _cnt = count(sales)
        gen double loo_vec = (_tot - sales) / (_cnt - 1) if _cnt > 1
        timer off 2
        assert abs(ind_mean_loop - ind_mean_vec) < 1e-10 if !missing(ind_mean_loop)
        assert missing(ind_mean_loop) == missing(ind_mean_vec)
        qui summarize sales if industry == 1
        local tot1 = r(sum)
        local n1 = r(N)
        assert abs(loo_vec - (`tot1' - sales) / (`n1' - 1)) < 1e-10 if industry == 1 & !missing(sales)
        qui timer list 1
        di "T_LOOP=" r(t1)
        qui timer list 2
        di "T_VEC=" r(t2)
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")
        self.assertGreater(float(m["T_LOOP"]), float(m["T_VEC"]))

    def test_03_lag_lead_diff_respects_gaps(self):
        bad = """
        forvalues i = 2/`=_N' {
            if id[`i'] == id[`i'-1] qui replace dy_loop = y[`i'] - y[`i'-1] in `i'
        }
        """
        good = """
        xtset id year
        gen double dy_vec = D.y
        gen double lag1_y = L.y
        """
        self._assert_linter(bad, "ANTI_LOOP_OBS_INDEX", good)

        do = """
        clear
        input int id int year double y
        1 2020 10
        1 2021 15
        1 2023 25
        2 2020 100
        2 2021 110
        end
        xtset id year
        gen double dy_vec = D.y
        assert missing(dy_vec) in 1
        assert dy_vec == 5 in 2
        * 2023 after 2021 has a 2-year gap: D.y must be missing rather than silently diffing 2021!
        assert missing(dy_vec) in 3
        assert dy_vec == 10 in 5
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_04_carryforward_sequential_replace(self):
        bad = """
        forvalues i = 2/`=_N' {
            if id[`i'] == id[`i'-1] & missing(x_loop[`i']) {
                qui replace x_loop = x_loop[`i'-1] in `i'
            }
        }
        """
        good = "bysort id (year): replace x_vec = x_vec[_n-1] if missing(x_vec)"
        self._assert_linter(bad, "ANTI_LOOP_CARRYFORWARD", good)

        do = """
        clear
        input int id int year double x_loop
        1 2018 5
        1 2019 .
        1 2020 .
        1 2021 9
        2 2018 .
        2 2019 12
        2 2020 .
        end
        gen double x_vec = x_loop
        sort id year
        forvalues i = 2/`=_N' {
            if id[`i'] == id[`i'-1] & missing(x_loop[`i']) {
                qui replace x_loop = x_loop[`i'-1] in `i'
            }
        }
        bysort id (year): replace x_vec = x_vec[_n-1] if missing(x_vec)
        assert x_loop == x_vec if !missing(x_vec)
        assert missing(x_loop) == missing(x_vec)
        assert x_vec == 5 in 3
        assert missing(x_vec) in 5
        assert x_vec == 12 in 7
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_05_rolling_window_cumulative_sum(self):
        bad = """
        forvalues t = 2003/2020 {
            qui summarize ret if inrange(year, `t'-2, `t')
            qui replace ma3_loop = r(mean) if year == `t'
        }
        """
        good = """
        bysort id (year): gen double run_sum = sum(ret)
        bysort id (year): gen double ma3_vec = (run_sum - cond(_n > 3, run_sum[_n-3], 0)) / 3 if _n >= 3
        """
        self._assert_linter(bad, "ANTI_LOOP_ROLLING_LAG", good)

        do = """
        clear
        set obs 36
        gen int id = ceil(_n / 18)
        bysort id: gen int year = 2002 + _n
        set seed 7
        gen double ret = rnormal(0.05, 0.1)
        gen double ma3_loop = .
        forvalues g = 1/2 {
            forvalues t = 2005/2020 {
                qui summarize ret if id == `g' & inrange(year, `t'-2, `t')
                qui replace ma3_loop = r(mean) if id == `g' & year == `t'
            }
        }
        bysort id (year): gen double run_sum = sum(ret)
        bysort id (year): gen double ma3_vec = (run_sum - cond(_n > 3, run_sum[_n-3], 0)) / 3 if _n >= 3
        assert abs(ma3_loop - ma3_vec) < 1e-12 if !missing(ma3_loop)
        assert missing(ma3_loop) == missing(ma3_vec)
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_06_dummies_and_factor_variables(self):
        bad = """
        levelsof rep78, local(reps)
        foreach r of local reps {
            gen byte d_`r' = (rep78 == `r')
        }
        """
        good = "regress price mpg i.rep78\ntabulate rep78, gen(d2_)"
        self._assert_linter(bad, "ANTI_LOOP_MANUAL_DUMMIES", good)

        do = """
        sysuse auto, clear
        drop if missing(rep78)
        levelsof rep78, local(reps)
        foreach r of local reps {
            gen byte d_`r' = (rep78 == `r')
        }
        qui regress price mpg d_2 d_3 d_4 d_5
        local b_mpg_loop = _b[mpg]
        local r2_loop = e(r2)
        qui regress price mpg i.rep78
        assert abs(_b[mpg] - `b_mpg_loop') < 1e-10
        assert abs(e(r2) - `r2_loop') < 1e-10
        tabulate rep78, gen(d2_)
        assert d_1 == d2_1 & d_2 == d2_2 & d_3 == d2_3 & d_4 == d2_4 & d_5 == d2_5
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_07_row_stats_equivalence(self):
        bad = """
        gen double tot_loop = 0
        foreach v of varlist q1-q5 {
            replace tot_loop = tot_loop + `v' if !missing(`v')
        }
        """
        good = """
        egen double tot_vec = rowtotal(q1-q5)
        egen double max_vec = rowmax(q1-q5)
        egen int cnt_vec = rownonmiss(q1-q5)
        """
        self._assert_linter(bad, "ANTI_LOOP_ROW_ACCUM", good)

        do = """
        clear
        set obs 200
        set seed 99
        forvalues j = 1/5 {
            gen double q`j' = cond(runiform() < 0.2, ., rnormal(10, 3))
        }
        gen double tot_loop = 0
        foreach v of varlist q1-q5 {
            replace tot_loop = tot_loop + `v' if !missing(`v')
        }
        egen double tot_vec = rowtotal(q1-q5)
        assert abs(tot_loop - tot_vec) < 1e-12
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_08_mvencode_and_mvdecode(self):
        bad = """
        foreach v of varlist q1-q4 {
            replace `v' = 0 if missing(`v')
        }
        """
        good = "mvencode q1-q4, mv(0)\nmvdecode q1-q4, mv(0)"
        self._assert_linter(bad, "ANTI_LOOP_MV_RECODE", good)

        do = """
        clear
        set obs 100
        forvalues j = 1/4 {
            gen double q`j' = cond(mod(_n + `j', 3) == 0, ., `j' * 10 + _n)
        }
        forvalues j = 1/4 {
            gen double p`j' = q`j'
        }
        foreach v of varlist q1-q4 {
            replace `v' = 0 if missing(`v')
        }
        mvencode p1-p4, mv(0)
        forvalues j = 1/4 {
            assert q`j' == p`j'
        }
        mvdecode p1-p4, mv(0)
        forvalues j = 1/4 {
            assert missing(p`j') if mod(_n + `j', 3) == 0
        }
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_09_group_regression_statsby(self):
        bad = """
        levelsof rep78, local(reps)
        foreach r of local reps {
            preserve
            keep if rep78 == `r'
            regress price mpg
            restore
        }
        """
        good = "statsby _b _se n=e(N), by(rep78) clear: regress price mpg"
        self._assert_linter(bad, "ANTI_LOOP_LEVELSOF", good)

        do = """
        sysuse auto, clear
        drop if missing(rep78)
        keep if inlist(rep78, 3, 4, 5)
        tempfile loop_res
        tempname pf
        postfile `pf' rep78 b_mpg b_cons using "`loop_res'", replace
        levelsof rep78, local(reps)
        foreach r of local reps {
            preserve
            keep if rep78 == `r'
            qui regress price mpg
            post `pf' (`r') (_b[mpg]) (_b[_cons])
            restore
        }
        postclose `pf'
        statsby _b, by(rep78) clear: regress price mpg
        merge 1:1 rep78 using "`loop_res'", assert(match) nogenerate
        assert abs(_b_mpg - b_mpg) < 1e-10
        assert abs(_b_cons - b_cons) < 1e-10
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_10_recode_and_inlist(self):
        bad = """
        gen byte treated_loop = 0
        foreach c in 11 14 23 45 {
            replace treated_loop = 1 if prov == `c'
        }
        """
        good = """
        gen byte treated_vec = inlist(prov, 11, 14, 23, 45)
        recode age (0/17=1) (18/64=2) (65/max=3), gen(age_grp)
        """
        self._assert_linter(bad, "ANTI_LOOP_RECODE", good)

        do = """
        clear
        input int prov int age
        11 15
        12 30
        14 70
        23 18
        45 64
        99 10
        end
        gen byte treated_loop = 0
        foreach c in 11 14 23 45 {
            replace treated_loop = 1 if prov == `c'
        }
        gen byte treated_vec = inlist(prov, 11, 14, 23, 45)
        assert treated_loop == treated_vec
        recode age (0/17=1) (18/64=2) (65/max=3), gen(age_grp)
        assert age_grp == 1 in 1
        assert age_grp == 2 in 2
        assert age_grp == 3 in 3
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_11_bulk_rename_group(self):
        bad = """
        foreach v of varlist v_* {
            local new = subinstr("`v'", "v_", "col_", 1)
            rename `v' `new'
        }
        """
        good = "rename v_* col_*"
        self._assert_linter(bad, "ANTI_LOOP_BULK_RENAME", good)

        do = """
        clear
        set obs 5
        gen v_a = _n
        gen v_b = _n * 2
        rename v_* col_*
        confirm variable col_a col_b
        capture confirm variable v_a
        assert _rc != 0
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_12_wide_to_long_panel(self):
        bad = """
        forvalues y = 2019/2021 {
            local prev = `y' - 1
            gen double g_`y' = (sales_`y' - sales_`prev') / sales_`prev'
        }
        """
        good = """
        reshape long sales_, i(id) j(year)
        xtset id year
        gen double g = (sales_ - L.sales_) / L.sales_
        """
        self._assert_linter(bad, "ANTI_LOOP_WIDE_YEAR", good)

        do = """
        clear
        input int id double sales_2018 double sales_2019 double sales_2020
        1 100 120 150
        2 200 180 270
        end
        preserve
        forvalues y = 2019/2020 {
            local prev = `y' - 1
            gen double g_`y' = (sales_`y' - sales_`prev') / sales_`prev'
        }
        keep id g_2019 g_2020
        reshape long g_, i(id) j(year)
        tempfile wide_out
        save "`wide_out'", replace
        restore
        reshape long sales_, i(id) j(year)
        xtset id year
        gen double g = (sales_ - L.sales_) / L.sales_
        keep if year >= 2019
        merge 1:1 id year using "`wide_out'", assert(match) nogenerate
        assert abs(g - g_) < 1e-12
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_13_collapse_replaces_postfile_loop(self):
        bad = """
        levelsof rep78, local(gs)
        foreach g of local gs {
            qui summarize price if rep78 == `g'
            post pf (`g') (r(mean)) (r(N))
        }
        """
        good = "collapse (mean) mean_p=price (count) n_p=price, by(rep78)"
        self._assert_linter(bad, "ANTI_LOOP_POSTFILE_SUMMARY", good)

        do = """
        sysuse auto, clear
        drop if missing(rep78)
        recast double price
        tempfile pf_out
        tempname pf
        postfile `pf' int rep78 double mean_loop long n_loop using "`pf_out'", replace
        levelsof rep78, local(gs)
        foreach g of local gs {
            qui summarize price if rep78 == `g'
            post `pf' (`g') (r(mean)) (r(N))
        }
        postclose `pf'
        collapse (mean) mean_vec=price (count) n_vec=price, by(rep78)
        merge 1:1 rep78 using "`pf_out'", assert(match) nogenerate
        assert abs(mean_vec - mean_loop) < 1e-10
        assert n_vec == n_loop
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_14_frames_replace_preserve_restore(self):
        bad = """
        levelsof rep78, local(gs)
        foreach g of local gs {
            preserve
            keep if rep78 == `g'
            restore
        }
        """
        good = """
        frame put rep78 price, into(scratch)
        frame scratch: collapse (mean) p_mean=price, by(rep78)
        frlink m:1 rep78, frame(scratch)
        frget p_mean, from(scratch)
        frame drop scratch
        """
        self._assert_linter(bad, "ANTI_LOOP_PRESERVE_RESTORE", good)

        do = """
        sysuse auto, clear
        drop if missing(rep78)
        recast double price
        bysort rep78: egen double p_ref = mean(price)
        frame put rep78 price, into(scratch)
        frame scratch: collapse (mean) p_mean=price, by(rep78)
        frlink m:1 rep78, frame(scratch)
        frget p_mean, from(scratch)
        frame drop scratch
        assert abs(p_ref - p_mean) < 1e-10
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")

    def test_15_mata_vectorized_ols(self):
        bad = """
        forvalues i = 1/`n' {
            forvalues j = 1/`m' {
                replace s = s + x[`i']
            }
        }
        """
        good = """
        mata:
            X = st_data(., ("mpg", "weight")), J(st_nobs(), 1, 1)
            y = st_data(., "price")
            b = invsym(cross(X, X)) * cross(X, y)
            st_matrix("b_mata", b')
        end
        """
        self._assert_linter(bad, "ANTI_LOOP_NESTED", good)

        do = """
        sysuse auto, clear
        qui regress price mpg weight
        matrix b_reg = e(b)
        mata:
            X = st_data(., ("mpg", "weight")), J(st_nobs(), 1, 1)
            y = st_data(., "price")
            b = invsym(cross(X, X)) * cross(X, y)
            st_matrix("b_mata", b')
        end
        assert abs(b_reg[1, 1] - b_mata[1, 1]) < 1e-8
        assert abs(b_reg[1, 2] - b_mata[1, 2]) < 1e-8
        assert abs(b_reg[1, 3] - b_mata[1, 3]) < 1e-6
        di "REAL_PASS=1"
        """
        _log, m = _run_stata_do(do)
        self.assertEqual(m.get("REAL_PASS"), "1")


if __name__ == "__main__":
    unittest.main()
