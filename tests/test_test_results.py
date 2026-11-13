import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import common  # noqa: E402
from orchestrator import test_commands, test_results  # noqa: E402

DOTNET_FAILED = """\
Test run for /p/tests/Daoc.Protocol.Tests.dll (.NETCoreApp,Version=v10.0)
[xUnit.net 00:00:00.25]     Daoc.Protocol.Tests.ClientConfigTests.Defaults [FAIL]
  Failed Daoc.Protocol.Tests.ClientConfigTests.Defaults [7 ms]
  Error Message:
   Assert.Equal() Failure: Strings differ
  Failed Daoc.Protocol.Tests.CharacterTests.Combo(classId: 43, raceId: 11) [< 1 ms]
Failed!  - Failed:     2, Passed:   387, Skipped:     1, Total:   390, Duration: 98 ms - Daoc.Protocol.Tests.dll (net10.0)
"""
DOTNET_PASSED = "Passed!  - Failed:     0, Passed:    24, Skipped:     0, Total:    24, Duration: 3 s - A.dll (net10.0)\n"

TRX = """﻿<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>
<UnitTestResult testName="Ns.ATests.Ok" outcome="Passed" />
<UnitTestResult testName="Ns.ATests.Bad(x: 1)" outcome="Failed" />
<UnitTestResult testName="Ns.ATests.Later" outcome="NotExecuted" />
</Results></TestRun>"""

JUNIT = """<testsuites><testsuite name="s">
<testcase classname="pkg.mod.Suite" name="test_ok"/>
<testcase classname="pkg.mod.Suite" name="test_bad"><failure message="x"/></testcase>
<testcase classname="pkg.mod.Suite" name="test_err"><error message="x"/></testcase>
<testcase classname="pkg.mod.Suite" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>"""


class DotnetOutputTests(unittest.TestCase):
    def test_counts_and_failing_names(self):
        r = test_results.parse_dotnet_output(DOTNET_FAILED)
        self.assertEqual((r["total_run"], r["failed_count"], r["skipped_count"], r["passed_count"]), (390, 2, 1, 387))
        self.assertEqual(r["failing_tests"], ["ClientConfigTests.Defaults", "CharacterTests.Combo"])

    def test_several_test_projects_add_up(self):
        r = test_results.parse_dotnet_output(DOTNET_PASSED + DOTNET_FAILED)
        self.assertEqual((r["total_run"], r["failed_count"]), (414, 2))

    def test_other_output_is_not_dotnet(self):
        self.assertIsNone(test_results.parse_dotnet_output("Ran 3 tests in 0.1s\nOK"))
        self.assertIsNone(test_results.parse_dotnet_output(""))

    def test_the_job_summary_parser_understands_it(self):
        parsed = common.parse_test_output(DOTNET_FAILED)
        self.assertEqual((parsed["total_run"], parsed["failed_count"], parsed["passed_count"]), (390, 2, 388))
        self.assertEqual(parsed["failing_tests"], ["ClientConfigTests.Defaults", "CharacterTests.Combo"])
        ok = common.parse_test_output(DOTNET_PASSED)
        self.assertEqual((ok["total_run"], ok["failed_count"], ok["failing_tests"]), (24, 0, []))


class XmlTests(unittest.TestCase):
    def test_trx(self):
        r = test_results.parse_trx(TRX)
        self.assertEqual((r["total_run"], r["passed_count"], r["failed_count"], r["skipped_count"]), (3, 1, 1, 1))
        self.assertEqual(r["failing_tests"], ["ATests.Bad"])

    def test_junit(self):
        r = test_results.parse_junit(JUNIT)
        self.assertEqual((r["total_run"], r["passed_count"], r["failed_count"], r["skipped_count"]), (4, 1, 2, 1))
        self.assertEqual(r["failing_tests"], ["Suite.test_bad", "Suite.test_err"])

    def test_a_single_testsuite_root_works_and_other_xml_does_not(self):
        self.assertEqual(test_results.parse_junit('<testsuite><testcase classname="a.B" name="c"/></testsuite>')["total_run"], 1)
        self.assertIsNone(test_results.parse_trx(JUNIT))
        self.assertIsNone(test_results.parse_junit(TRX))
        self.assertIsNone(test_results.parse_junit("not xml"))

    def test_entity_declarations_are_refused(self):
        bomb = '<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase classname="&a;" name="n"/></testsuite>'
        self.assertIsNone(test_results.parse_junit(bomb))


class ReadResultsTests(unittest.TestCase):
    def test_globs_combine_files_and_skip_junk_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / "out").mkdir(parents=True)
            (root / "out" / "a.trx").write_text(TRX, encoding="utf-8")
            (root / "out" / "b.xml").write_text(JUNIT, encoding="utf-8")
            (root / "out" / "c.xml").write_text("junk", encoding="utf-8")
            (Path(tmp) / "outside.xml").write_text(JUNIT, encoding="utf-8")
            r = test_results.read_results(root, "out/*")
            self.assertEqual((r["total_run"], r["failed_count"]), (7, 3))
            self.assertIsNone(test_results.read_results(root, "missing/*.trx"))
            self.assertIsNone(test_results.read_results(root, "../outside.xml"))


class CommandCountTests(unittest.TestCase):
    def test_a_result_carries_counts_from_dotnet_output_or_a_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.xml").write_text(JUNIT, encoding="utf-8")
            no_req = lambda r, p: []  # noqa: E731
            dotnet = test_commands.run_one(test_commands.ExtraTestCommand("a", "x"), root,
                                           run=lambda c, d, t: (1, DOTNET_FAILED, False), requirements=no_req)
            self.assertEqual(dotnet["tests"]["total_run"], 390)
            self.assertEqual(dotnet["failing_tests"][0], "ClientConfigTests.Defaults")
            self.assertIn("390 tests, 2 failed, 1 skipped", test_commands.summary([dotnet]))
            filed = test_commands.run_one(test_commands.ExtraTestCommand("b", "x", results="r.xml"), root,
                                          run=lambda c, d, t: (0, "no counts printed", False), requirements=no_req)
            self.assertEqual(filed["tests"]["failed_count"], 2)

    def test_results_must_stay_inside_the_project(self):
        _, errors = test_commands.parse([{"name": "a", "command": "x", "results": "../x.trx"},
                                         {"name": "b", "command": "x", "results": "/etc/x.xml"},
                                         {"name": "c", "command": "x", "results": 5}])
        self.assertEqual(len(errors), 3)
        ok, errors = test_commands.parse([{"name": "a", "command": "x", "results": "TestResults/*.trx"}])
        self.assertEqual((errors, ok[0].results), ([], "TestResults/*.trx"))


if __name__ == "__main__":
    unittest.main()
