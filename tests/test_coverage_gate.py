#!/usr/bin/env python3
"""The coverage gate, tested against tiny synthetic projects.

Two things make this file's shape unusual, and both are deliberate.

`measure()` runs through a SUBPROCESS. The gate installs a `sys.monitoring`
tool, and this test module is itself part of the suite the gate traces — so when
the verifier runs `python3 scripts/coverage_gate.py`, an in-process `measure()`
here would try to claim a tool id the outer run already holds. A subprocess is
immune to that, and it is also how the gate is actually invoked.

The pure functions — line discovery, the exclusion rules, the report arithmetic,
the exit codes — are exercised directly, because those are where a gate quietly
stops gating. A denominator rule that silently swallows a whole module reads
exactly like a module with perfect coverage.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts import coverage_gate

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "coverage_gate.py"


# ─── Synthetic projects ──────────────────────────────────────────────────────

ALPHA = '''\
"""A module with a covered branch, a dead function, and two excluded blocks."""

import argparse
import sys


def covered(value):
    if value > 0:
        return "positive"
    return "other"


def never_called():
    total = 0
    for index in range(3):
        total += index
    return total


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args(argv)
    if args.show:
        print("live snapshot")
        print("never reachable from a test")
        return 0
    if args.export:
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

TEST_ALPHA = '''\
import unittest

from scripts.alpha import covered


class AlphaTests(unittest.TestCase):
    def test_both_branches(self):
        self.assertEqual(covered(1), "positive")
        self.assertEqual(covered(-1), "other")
'''

# Named timefmt.py so it lands on a REAL floor in coverage_gate.FLOORS (100%)
# and can be driven below it; every other synthetic module is ungoverned.
TIMEFMT = '''\
def half(value):
    if value:
        return 1
    return 0


def never_used():
    return 2
'''

TEST_TIMEFMT = '''\
import unittest

from scripts.timefmt import half


class HalfTests(unittest.TestCase):
    def test_half(self):
        self.assertEqual(half(1), 1)
'''

BETA = '''\
def orphan():
    return "nothing imports me"
'''

FAILING_TEST = '''\
import unittest


class FailingTests(unittest.TestCase):
    def test_it_fails(self):
        self.assertEqual(1, 2)
'''


def write_project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ai-resets-cov-"))
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def run_gate(root: Path, *flags: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), "--json", *flags],
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        return completed.returncode, json.loads(completed.stdout)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken gate
        raise AssertionError(
            f"gate produced no JSON (exit {completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        ) from None


def module(report: dict, name: str) -> dict:
    for entry in report["modules"]:
        if entry["module"] == name:
            return entry
    raise AssertionError(f"{name} is not in the report: {[m['module'] for m in report['modules']]}")


def remove_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


# ─── Executable lines ────────────────────────────────────────────────────────


class ExecutableLineTests(unittest.TestCase):
    def test_only_lines_with_bytecode_count(self):
        source = textwrap.dedent(
            '''\
            """docstring"""
            # a comment

            X = 1


            def f():
                return X
            '''
        )
        lines = coverage_gate.executable_lines(source, "<synthetic>")
        self.assertIn(4, lines, "the assignment is a statement")
        self.assertIn(7, lines, "the def is executed at import")
        self.assertIn(8, lines, "the return is a statement")
        self.assertNotIn(2, lines, "a comment carries no bytecode")
        self.assertNotIn(3, lines, "a blank line carries no bytecode")

    def test_nested_code_objects_are_walked(self):
        # Comprehensions, lambdas and methods live in their own code objects.
        # Missing them would shrink the denominator and inflate every module
        # that has any — which is every module here.
        source = textwrap.dedent(
            """\
            def outer():
                def inner():
                    return 1
                return [inner() for _ in range(2)]
            """
        )
        self.assertEqual(coverage_gate.executable_lines(source, "<synthetic>"), {1, 2, 3, 4})

    def test_the_denominator_is_a_superset_of_anything_reachable(self):
        # The property the module claims: numerator and denominator come from
        # the same line table, so coverage can never exceed 100%.
        source = ALPHA
        lines = coverage_gate.executable_lines(source, "<synthetic>")
        compiled = compile(source, "<synthetic>", "exec")
        self.assertTrue({line for _, _, line in compiled.co_lines() if line} <= lines)


# ─── Exclusion rules ─────────────────────────────────────────────────────────


class ExclusionTests(unittest.TestCase):
    def rules(self, source: str) -> dict[str, set[int]]:
        return coverage_gate.excluded_lines(source, "<synthetic>")

    def test_the_main_guard_and_its_body_are_excluded(self):
        rules = self.rules(ALPHA)
        lines = sorted(rules["main-tail"])
        self.assertEqual(len(lines), 2, "the guard line and the sys.exit call")
        self.assertIn('if __name__ == "__main__":', ALPHA.splitlines()[lines[0] - 1])

    def test_a_debug_branch_body_is_excluded_but_its_guard_is_not(self):
        rules = self.rules(ALPHA)
        body = sorted(rules["debug-branch"])
        source_lines = ALPHA.splitlines()
        self.assertEqual(len(body), 3, "two prints and a return")
        self.assertNotIn(
            "if args.show:",
            [source_lines[line - 1] for line in body],
            "the guard stays in the denominator so the branch is still gated",
        )
        self.assertIn("live snapshot", source_lines[body[0] - 1])

    def test_a_non_debug_flag_branch_is_not_excluded(self):
        # `--export` is testable without a network call and is covered by the
        # real suite; only flags listed in DEBUG_FLAGS are forgiven.
        excluded = set().union(*self.rules(ALPHA).values())
        export_line = next(
            i + 1 for i, line in enumerate(ALPHA.splitlines()) if "return 3" in line
        )
        self.assertNotIn(export_line, excluded)

    def test_only_the_exact_main_comparison_matches(self):
        for source in (
            'if __name__ != "__main__":\n    pass\n',
            'if __name__ == "__init__":\n    pass\n',
            'if name == "__main__":\n    pass\n',
            'if __name__ == "__main__" and True:\n    pass\n',
        ):
            with self.subTest(source=source.splitlines()[0]):
                self.assertEqual(self.rules(source)["main-tail"], set())

    def test_only_args_attributes_match_the_debug_rule(self):
        for source in (
            "if show:\n    pass\n",
            "if other.show:\n    pass\n",
            "if args.showing:\n    pass\n",
            "if args.show():\n    pass\n",
        ):
            with self.subTest(source=source.splitlines()[0]):
                self.assertEqual(self.rules(source)["debug-branch"], set())

    def test_a_module_with_neither_rule_excludes_nothing(self):
        rules = self.rules("X = 1\n")
        self.assertEqual(rules, {"main-tail": set(), "debug-branch": set()})

    def test_debug_flags_lists_exactly_what_it_documents(self):
        # The exclusion list is the part of a coverage gate most likely to grow
        # quietly. Adding an entry has to be a decision someone made here.
        self.assertEqual(coverage_gate.DEBUG_FLAGS, frozenset({"show"}))


# ─── Report arithmetic ───────────────────────────────────────────────────────


class ModuleReportTests(unittest.TestCase):
    def report(self, **kwargs) -> coverage_gate.ModuleReport:
        base: dict[str, object] = {
            "name": "timefmt.py", "covered": 9, "total": 10, "floor": 90.0
        }
        base.update(kwargs)
        return coverage_gate.ModuleReport(**base)  # type: ignore[arg-type]

    def test_percent(self):
        self.assertEqual(self.report().percent, 90.0)

    def test_a_module_with_no_executable_lines_is_not_a_division_by_zero(self):
        # `scripts/__init__.py` is skipped by name, but a module that is nothing
        # but a docstring would otherwise crash the whole gate.
        self.assertEqual(self.report(covered=0, total=0).percent, 100.0)

    def test_exactly_at_the_floor_passes(self):
        self.assertEqual(self.report(covered=9, total=10, floor=90.0).status, "ok")

    def test_a_hair_under_the_floor_fails(self):
        self.assertEqual(self.report(covered=8, total=10, floor=90.0).status, "BELOW")

    def test_a_module_with_no_declared_floor_is_ungoverned(self):
        self.assertEqual(self.report(name="brand_new.py").status, "UNGOVERNED")

    def test_as_dict_carries_what_a_fixer_needs(self):
        payload = self.report(excluded={"main-tail": 2}, has_tests=False).as_dict()
        self.assertEqual(payload["module"], "timefmt.py")
        self.assertEqual(payload["percent"], 90.0)
        self.assertEqual(payload["excluded"], {"main-tail": 2})
        self.assertFalse(payload["has_tests"])


class ExitCodeTests(unittest.TestCase):
    def report(self, **kwargs) -> coverage_gate.Report:
        base: dict[str, object] = {
            "modules": [], "tests_run": 1, "failures": [], "errors": [], "seconds": 0.1
        }
        base.update(kwargs)
        return coverage_gate.Report(**base)  # type: ignore[arg-type]

    def code(self, report, *, strict=False, coverage_only=False) -> int:
        return coverage_gate.exit_code(report, strict=strict, coverage_only=coverage_only)

    def test_a_clean_run_is_zero(self):
        self.assertEqual(self.code(self.report()), 0)

    def test_below_floor_is_one(self):
        below = coverage_gate.ModuleReport("timefmt.py", 1, 10, 90.0)
        self.assertEqual(self.code(self.report(modules=[below])), 1)

    def test_a_failing_suite_is_two_not_one(self):
        # Split on purpose: "coverage dropped" and "the tests do not run" need
        # different fixes, and one shared exit code sends the reader to the
        # wrong one.
        self.assertEqual(self.code(self.report(failures=["t"])), 2)
        self.assertEqual(self.code(self.report(errors=["t"])), 2)

    def test_an_unparsable_module_is_two(self):
        self.assertEqual(self.code(self.report(skipped_modules=["x.py: unparsable"])), 2)

    def test_coverage_only_ignores_a_failing_suite(self):
        self.assertEqual(self.code(self.report(failures=["t"]), coverage_only=True), 0)

    def test_coverage_only_still_enforces_the_floor(self):
        below = coverage_gate.ModuleReport("timefmt.py", 1, 10, 90.0)
        self.assertEqual(self.code(self.report(modules=[below]), coverage_only=True), 1)

    def test_ungoverned_modules_pass_by_default_and_fail_under_strict(self):
        new = coverage_gate.ModuleReport("brand_new.py", 5, 10, 0.0)
        self.assertEqual(self.code(self.report(modules=[new])), 0)
        self.assertEqual(self.code(self.report(modules=[new]), strict=True), 1)

    def test_untested_modules_fail_only_under_strict(self):
        governed = coverage_gate.ModuleReport("timefmt.py", 10, 10, 100.0, has_tests=False)
        self.assertEqual(self.code(self.report(modules=[governed])), 0)
        self.assertEqual(self.code(self.report(modules=[governed]), strict=True), 1)


class RenderTests(unittest.TestCase):
    def test_every_kind_of_problem_gets_its_own_line(self):
        report = coverage_gate.Report(
            modules=[
                coverage_gate.ModuleReport("timefmt.py", 1, 10, 90.0, {"main-tail": 2}),
                coverage_gate.ModuleReport("brand_new.py", 5, 10, 0.0, has_tests=False),
            ],
            tests_run=3,
            failures=["test_a"],
            errors=["test_b"],
            seconds=1.25,
            skipped_modules=["half_written.py: unparsable (invalid syntax line 4)"],
        )
        text = coverage_gate.render(report)
        for expected in (
            "BELOW    timefmt.py",
            "UNGOVERNED brand_new.py",
            "GAP      brand_new.py has no tests/test_brand_new.py",
            "SKIPPED  half_written.py",
            "FAILED   test_a",
            "ERROR    test_b",
            "(-2 excluded)",
            "3 tests, 1 failures, 1 errors",
        ):
            with self.subTest(line=expected):
                self.assertIn(expected, text)


# ─── Module discovery ────────────────────────────────────────────────────────


class SourceModuleTests(unittest.TestCase):
    def test_the_list_is_read_from_disk_not_hard_coded(self):
        root = write_project({"scripts/zeta.py": "X = 1\n"})
        self.addCleanup(remove_tree, root)
        self.assertEqual([p.name for p in coverage_gate.source_modules(root)], ["zeta.py"])

    def test_the_package_marker_and_the_gate_itself_are_skipped(self):
        root = write_project(
            {"scripts/coverage_gate.py": "X = 1\n", "scripts/zeta.py": "X = 1\n"}
        )
        self.addCleanup(remove_tree, root)
        names = [p.name for p in coverage_gate.source_modules(root)]
        self.assertEqual(names, ["zeta.py"])
        self.assertNotIn("__init__.py", names)

    def test_the_real_repository_is_walked(self):
        names = [p.name for p in coverage_gate.source_modules(ROOT)]
        self.assertIn("timefmt.py", names)
        self.assertIn("quota_probe.py", names)
        self.assertNotIn("coverage_gate.py", names)


# ─── The line recorder ───────────────────────────────────────────────────────


class LineRecorderTests(unittest.TestCase):
    def test_a_busy_tool_id_is_refused_loudly(self):
        # Silently measuring nothing would report 0% for every module, which
        # reads like a catastrophic regression instead of a broken instrument.
        monitoring = sys.monitoring
        if monitoring.get_tool(coverage_gate.TOOL_ID) is None:
            monitoring.use_tool_id(coverage_gate.TOOL_ID, "test-holder")
            self.addCleanup(monitoring.free_tool_id, coverage_gate.TOOL_ID)
        with self.assertRaises(RuntimeError) as caught:
            with coverage_gate.LineRecorder([]):
                pass
        self.assertIn("already in use", str(caught.exception))

    def test_it_records_the_lines_a_call_actually_runs(self):
        if sys.monitoring.get_tool(coverage_gate.TOOL_ID) is not None:
            self.skipTest("a coverage run already holds the tool id; see the module docstring")
        root = write_project({"scripts/probe_target.py": "def f(flag):\n    if flag:\n        return 1\n    return 2\n"})
        self.addCleanup(remove_tree, root)
        target = root / "scripts" / "probe_target.py"
        namespace: dict = {}
        code = compile(target.read_text(), str(target.resolve()), "exec")
        exec(code, namespace)
        with coverage_gate.LineRecorder([target]) as recorder:
            namespace["f"](True)
        hits = recorder.hits[str(target.resolve())]
        self.assertIn(2, hits, "the branch test ran")
        self.assertIn(3, hits, "the True arm ran")
        self.assertNotIn(4, hits, "the False arm did not")


# ─── End to end, through a subprocess ────────────────────────────────────────


class MeasuredProjectTests(unittest.TestCase):
    """One healthy synthetic project, measured once and asserted many ways."""

    root: Path
    code: int
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = write_project(
            {
                "scripts/alpha.py": ALPHA,
                "scripts/beta.py": BETA,
                "tests/test_alpha.py": TEST_ALPHA,
            }
        )
        cls.code, cls.report = run_gate(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        remove_tree(cls.root)

    def test_the_suite_actually_ran(self):
        self.assertEqual(self.report["tests_run"], 1)
        self.assertEqual(self.report["failures"], [])
        self.assertEqual(self.report["errors"], [])

    def test_ungoverned_modules_do_not_fail_the_default_gate(self):
        self.assertEqual(self.code, 0)
        self.assertEqual(sorted(self.report["ungoverned"]), ["alpha.py", "beta.py"])

    def test_a_module_with_no_test_file_is_a_reported_gap_not_a_crash(self):
        # The brief's requirement: other lanes add modules to this tree while
        # the gate runs, and a missing test file must be a line in the report.
        self.assertEqual(self.report["untested"], ["beta.py"])
        self.assertFalse(module(self.report, "beta.py")["has_tests"])
        self.assertEqual(module(self.report, "beta.py")["covered"], 0)

    def test_covered_and_uncovered_functions_are_told_apart(self):
        alpha = module(self.report, "alpha.py")
        self.assertGreater(alpha["percent"], 0)
        self.assertLess(alpha["percent"], 100, "never_called() must not read as covered")

    def test_the_exclusions_are_counted_in_the_report(self):
        self.assertEqual(
            module(self.report, "alpha.py")["excluded"], {"main-tail": 2, "debug-branch": 3}
        )

    def test_the_excluded_lines_really_left_the_denominator(self):
        alpha = module(self.report, "alpha.py")
        executable = coverage_gate.executable_lines(ALPHA, "<synthetic>")
        dropped = set().union(*coverage_gate.excluded_lines(ALPHA, "<synthetic>").values())
        self.assertEqual(alpha["total"], len(executable - dropped))
        self.assertEqual(len(dropped), 5)

    def test_coverage_never_exceeds_one_hundred_percent(self):
        for entry in self.report["modules"]:
            with self.subTest(module=entry["module"]):
                self.assertLessEqual(entry["covered"], entry["total"])

    def test_it_finishes_well_under_a_minute(self):
        self.assertLess(self.report["seconds"], 60)


class GateOutcomeTests(unittest.TestCase):
    def build(self, files: dict[str, str]) -> Path:
        root = write_project(files)
        self.addCleanup(remove_tree, root)
        return root

    def test_a_governed_module_under_its_floor_exits_one(self):
        root = self.build({"scripts/timefmt.py": TIMEFMT, "tests/test_timefmt.py": TEST_TIMEFMT})
        code, report = run_gate(root)
        self.assertEqual(code, 1)
        self.assertEqual(report["below_floor"], ["timefmt.py"])
        self.assertEqual(module(report, "timefmt.py")["floor"], coverage_gate.FLOORS["timefmt.py"])

    def test_strict_turns_an_ungoverned_module_into_a_failure(self):
        root = self.build({"scripts/alpha.py": ALPHA, "tests/test_alpha.py": TEST_ALPHA})
        self.assertEqual(run_gate(root)[0], 0)
        self.assertEqual(run_gate(root, "--strict")[0], 1)

    def test_a_failing_test_exits_two_and_names_the_case(self):
        root = self.build({"scripts/alpha.py": ALPHA, "tests/test_broken.py": FAILING_TEST})
        code, report = run_gate(root)
        self.assertEqual(code, 2)
        self.assertTrue(any("test_it_fails" in case for case in report["failures"]), report)

    def test_coverage_only_reports_the_failure_but_gates_on_coverage(self):
        root = self.build({"scripts/alpha.py": ALPHA, "tests/test_broken.py": FAILING_TEST})
        code, report = run_gate(root, "--coverage-only")
        self.assertEqual(code, 0)
        self.assertEqual(len(report["failures"]), 1, "the failure is still reported")

    def test_a_half_written_module_is_skipped_not_a_crash(self):
        # Four other agents are editing this worktree; one of them saving a file
        # mid-edit must not make the gate look like a catastrophic failure.
        root = self.build(
            {
                "scripts/alpha.py": ALPHA,
                "scripts/half_written.py": "def broken(:\n    pass\n",
                "tests/test_alpha.py": TEST_ALPHA,
            }
        )
        code, report = run_gate(root)
        self.assertEqual(code, 2)
        self.assertEqual(len(report["skipped_modules"]), 1)
        self.assertIn("half_written.py", report["skipped_modules"][0])
        self.assertIn("unparsable", report["skipped_modules"][0])
        # ...and the healthy module was still measured.
        self.assertGreater(module(report, "alpha.py")["percent"], 0)

    def test_module_level_lines_are_counted_as_covered(self):
        # Regression, found by this gate measuring the real repository on
        # 2026-09-07: `TestLoader.discover` IMPORTS every test module, so with
        # discovery outside the tracer each module's whole top level ran
        # untraced and was counted as uncovered (timefmt.py read 66.7% while
        # its every line was exercised). This synthetic module is nothing BUT
        # top-level statements: it reads 100% when the gate is right and 0%
        # when that bug is back.
        root = self.build(
            {
                "scripts/constants.py": "ALPHA = 1\nBETA = ALPHA + 1\n",
                "tests/test_constants.py": (
                    "import unittest\n\nfrom scripts.constants import BETA\n\n\n"
                    "class ConstantTests(unittest.TestCase):\n"
                    "    def test_it(self):\n        self.assertEqual(BETA, 2)\n"
                ),
            }
        )
        code, report = run_gate(root)
        self.assertEqual(code, 0)
        entry = module(report, "constants.py")
        self.assertEqual((entry["covered"], entry["total"]), (2, 2))

    def test_a_project_with_no_modules_at_all_is_not_a_crash(self):
        root = self.build({})
        code, report = run_gate(root)
        self.assertEqual(code, 0)
        self.assertEqual(report["modules"], [])


class FloorDeclarationTests(unittest.TestCase):
    """The floors are a contract; these keep them honest."""

    def test_no_floor_names_a_module_that_is_gone(self):
        # A floor left behind by a deleted module is a contract line nothing
        # enforces, and it makes the table look more governed than it is.
        on_disk = {path.name for path in coverage_gate.source_modules(ROOT)}
        self.assertEqual(
            set(coverage_gate.FLOORS) - on_disk, set(), "these floors name modules that are gone"
        )

    def test_floors_are_percentages(self):
        for name, floor in coverage_gate.FLOORS.items():
            with self.subTest(module=name):
                self.assertGreaterEqual(floor, 0.0)
                self.assertLessEqual(floor, 100.0)

    def test_the_margin_is_documented_and_small(self):
        self.assertEqual(coverage_gate.FLOOR_MARGIN, 3)

    def test_every_floor_is_its_recorded_measurement_minus_the_margin(self):
        # A floor with no measured number beside it is a guess, and a floor that
        # does not follow from its own recorded measurement is a hand-edit that
        # the table no longer explains. Both are caught here rather than by
        # someone wondering, months later, where 81 came from.
        import re

        source = (ROOT / "scripts" / "coverage_gate.py").read_text(encoding="utf-8")
        block = source.split("FLOORS: dict[str, float] = {")[1].split("\n}")[0]
        for name, floor in coverage_gate.FLOORS.items():
            with self.subTest(module=name):
                line = next(l for l in block.splitlines() if f'"{name}"' in l)
                match = re.search(r"#\s*(\d+(?:\.\d+)?)\s*$", line)
                self.assertIsNotNone(match, f"{name} records no measured number")
                measured = float(match.group(1))
                self.assertEqual(
                    floor,
                    float(int(measured) - coverage_gate.FLOOR_MARGIN),
                    f"{name}: floor {floor} does not follow from measured {measured}",
                )


if __name__ == "__main__":
    unittest.main()
