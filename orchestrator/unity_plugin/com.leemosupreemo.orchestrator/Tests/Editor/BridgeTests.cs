using System;
using System.Collections.Generic;
using NUnit.Framework;

namespace Orchestrator.UnityBridge.Tests
{
    public class BridgeTests
    {
        private static ConsoleEntry Entry(EntryKind kind, string message, string stack = "")
        {
            return new ConsoleEntry { Kind = kind, Message = message, StackTrace = stack };
        }

        [Test]
        public void QuoteEscapesWhatBreaksJson()
        {
            Assert.AreEqual("\"a\\\"b\\\\c\\nd\\u0001\"", JsonText.Quote("a\"b\\c\nd\u0001"));
            Assert.AreEqual("\"\"", JsonText.Quote(null));
        }

        [Test]
        public void NewBugCarriesTheFieldsTheServerReads()
        {
            var body = JobRequest.NewBug("Score resets", "1. Play", "Keeps score", "console text", false);
            StringAssert.Contains("\"action\":\"new_job\"", body);
            StringAssert.Contains("\"type\":\"bug\"", body);
            StringAssert.Contains("\"summary\":\"Score resets\"", body);
            StringAssert.Contains("\"spec\":\"console text\"", body);
            StringAssert.Contains("\"no_dispatch\":true", body);
            StringAssert.Contains("\"no_dispatch\":false", JobRequest.NewBug("x", "", "", "", true));
        }

        [Test]
        public void NewBugCutsTextTheServerWouldRefuse()
        {
            var body = JobRequest.NewBug(new string('a', 900), "", "", "", false);
            StringAssert.Contains(new string('a', JobRequest.SummaryLimit) + "\"", body);
            StringAssert.DoesNotContain(new string('a', JobRequest.SummaryLimit + 1), body);
        }

        [Test]
        public void DigestKeepsOnlyProblemsAndFoldsRepeats()
        {
            var entries = new List<ConsoleEntry>
            {
                Entry(EntryKind.Log, "hello"),
                Entry(EntryKind.Warning, "slow"),
                Entry(EntryKind.Error, "NullReferenceException", "Score.Add () (at Assets/Score.cs:12)"),
                Entry(EntryKind.Error, "NullReferenceException", "Score.Add () (at Assets/Score.cs:12)"),
                Entry(EntryKind.Exception, "boom"),
            };
            var problems = ConsoleDigest.Problems(entries);
            Assert.AreEqual(2, problems.Count);
            Assert.AreEqual(2, problems[0].Count);
            var text = ConsoleDigest.Build(entries, 10000);
            StringAssert.Contains("2 problems", text);
            StringAssert.Contains("Error x2: NullReferenceException", text);
            StringAssert.Contains("Assets/Score.cs:12", text);
            StringAssert.DoesNotContain("hello", text);
            Assert.AreEqual("", ConsoleDigest.Build(new List<ConsoleEntry> { Entry(EntryKind.Log, "x") }, 100));
        }

        [Test]
        public void DigestDropsTheOldestWhenTooLong()
        {
            var entries = new List<ConsoleEntry>();
            for (var i = 0; i < 20; i++) entries.Add(Entry(EntryKind.Error, "problem number " + i));
            var text = ConsoleDigest.Build(entries, 200);
            StringAssert.Contains("problem number 19", text);
            StringAssert.DoesNotContain("problem number 0\n", text);
            StringAssert.Contains("older not shown", text);
        }

        [Test]
        public void PlainHttpIsOnlyForThisComputer()
        {
            Assert.AreEqual("http://127.0.0.1:8765", Connection.NormalizeUrl(""));
            Assert.AreEqual("http://localhost:8765", Connection.NormalizeUrl("http://localhost:8765/"));
            Assert.AreEqual("https://mac.example.com", Connection.NormalizeUrl("mac.example.com"));
            Assert.Throws<ArgumentException>(() => Connection.NormalizeUrl("http://mac.example.com"));
            Assert.Throws<ArgumentException>(() => Connection.NormalizeUrl("https://me:pw@mac.example.com"));
            Assert.Throws<ArgumentException>(() => Connection.NormalizeUrl("ftp://mac.example.com"));
        }

        [Test]
        public void EndpointsAndJobPagesJoinCleanly()
        {
            Assert.AreEqual("http://127.0.0.1:8765/api/jobs", Connection.Endpoint("http://127.0.0.1:8765/", "/api/jobs"));
            Assert.AreEqual("http://127.0.0.1:8765/#/jobs/a%20b", Connection.JobPage("", "a b"));
        }

        [Test]
        public void MissingTokenFileMeansNoToken()
        {
            Assert.AreEqual("", Connection.ReadLocalToken(System.IO.Path.Combine(System.IO.Path.GetTempPath(), "no-such-home-" + Guid.NewGuid())));
        }

        [Test]
        public void FailingTestsAreListedWithTheirMessages()
        {
            TestFailures.Clear();
            Assert.AreEqual("", TestFailures.Build(1000));
            TestFailures.Add("Score.Tests.Adds", "Expected 3 but was 2", "at Score.Tests.Adds ()");
            var text = TestFailures.Build(1000);
            StringAssert.Contains("Failing Unity tests (1)", text);
            StringAssert.Contains("Score.Tests.Adds", text);
            StringAssert.Contains("Expected 3 but was 2", text);
            TestFailures.Clear();
        }
    }
}
