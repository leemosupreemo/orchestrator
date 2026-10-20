using System;
using System.Collections.Generic;
using System.Text;

namespace Orchestrator.UnityBridge
{
    internal enum EntryKind { Log, Warning, Error, Assert, Exception }

    internal sealed class ConsoleEntry
    {
        internal EntryKind Kind;
        internal string Message = "";
        internal string StackTrace = "";
        internal int Count = 1;

        internal bool IsProblem => Kind != EntryKind.Log && Kind != EntryKind.Warning;
    }

    /// <summary>Turns recorded console entries into the text that goes into a job.</summary>
    internal static class ConsoleDigest
    {
        internal const int StackLines = 8;

        /// <summary>Errors, asserts and exceptions only, identical ones folded into a count, newest last.</summary>
        internal static List<ConsoleEntry> Problems(IEnumerable<ConsoleEntry> entries)
        {
            var order = new List<ConsoleEntry>();
            var seen = new Dictionary<string, ConsoleEntry>();
            foreach (var entry in entries)
            {
                if (!entry.IsProblem) continue;
                var key = entry.Kind + "\n" + entry.Message + "\n" + entry.StackTrace;
                if (seen.TryGetValue(key, out var existing))
                {
                    existing.Count += entry.Count;
                    order.Remove(existing);       // a repeat is the newest occurrence now
                    order.Add(existing);
                    continue;
                }
                var copy = new ConsoleEntry { Kind = entry.Kind, Message = entry.Message, StackTrace = entry.StackTrace, Count = entry.Count };
                seen[key] = copy;
                order.Add(copy);
            }
            return order;
        }

        /// <summary>Markdown for the job. When it is too long the oldest problems are dropped, never the newest.</summary>
        internal static string Build(IEnumerable<ConsoleEntry> entries, int maxChars)
        {
            var problems = Problems(entries);
            if (problems.Count == 0) return "";
            var blocks = new List<string>();
            foreach (var entry in problems) blocks.Add(Block(entry));

            var kept = new List<string>();
            var length = 0;
            for (var i = blocks.Count - 1; i >= 0; i--)
            {
                if (kept.Count > 0 && length + blocks[i].Length > maxChars) break;
                kept.Insert(0, blocks[i]);
                length += blocks[i].Length + 1;
            }
            var dropped = blocks.Count - kept.Count;
            var text = new StringBuilder();
            text.Append("### Unity console (").Append(problems.Count).Append(problems.Count == 1 ? " problem" : " problems").Append(")\n");
            if (dropped > 0) text.Append("(").Append(dropped).Append(" older not shown)\n");
            text.Append("```\n").Append(string.Join("\n", kept)).Append("\n```");
            return text.ToString();
        }

        private static string Block(ConsoleEntry entry)
        {
            var text = new StringBuilder();
            text.Append(entry.Kind).Append(entry.Count > 1 ? " x" + entry.Count : "").Append(": ").Append(entry.Message.Trim());
            var shown = 0;
            foreach (var line in (entry.StackTrace ?? "").Split('\n'))
            {
                var trimmed = line.Trim();
                if (trimmed.Length == 0) continue;
                if (shown++ == StackLines) { text.Append("\n  ..."); break; }
                text.Append("\n  ").Append(trimmed);
            }
            return text.ToString();
        }
    }
}
