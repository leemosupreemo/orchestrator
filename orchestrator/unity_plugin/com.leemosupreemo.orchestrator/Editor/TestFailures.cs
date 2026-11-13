using System.Collections.Generic;

namespace Orchestrator.UnityBridge
{
    /// <summary>
    /// The failing tests of the latest Test Runner run. The recorder in the TestRunner folder fills this in when the
    /// Unity Test Framework is installed; without it the list stays empty.
    /// </summary>
    public static class TestFailures
    {
        private static readonly List<string> Names = new List<string>();
        private static readonly List<string> Details = new List<string>();

        public static void Clear()
        {
            lock (Names) { Names.Clear(); Details.Clear(); }
        }

        public static void Add(string name, string message, string stackTrace)
        {
            lock (Names)
            {
                Names.Add(name ?? "");
                Details.Add(((message ?? "").Trim() + "\n" + (stackTrace ?? "").Trim()).Trim());
            }
        }

        internal static int Count
        {
            get { lock (Names) return Names.Count; }
        }

        /// <summary>Markdown for the job, one block per failed test.</summary>
        internal static string Build(int maxChars)
        {
            lock (Names)
            {
                if (Names.Count == 0) return "";
                var text = new System.Text.StringBuilder();
                text.Append("### Failing Unity tests (").Append(Names.Count).Append(")\n");
                for (var i = 0; i < Names.Count; i++)
                {
                    var block = "- `" + Names[i] + "`\n```\n" + Details[i] + "\n```\n";
                    if (text.Length + block.Length > maxChars)
                    {
                        text.Append("(").Append(Names.Count - i).Append(" more not shown)");
                        break;
                    }
                    text.Append(block);
                }
                return text.ToString().TrimEnd();
            }
        }
    }
}
