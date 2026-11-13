using System.Collections.Generic;
using UnityEditor;
using UnityEngine;

namespace Orchestrator.UnityBridge
{
    /// <summary>Remembers the console's recent messages, so "send what's in the console" works after the fact.</summary>
    [InitializeOnLoad]
    internal static class ConsoleRecorder
    {
        private const int Capacity = 300;
        private static readonly object Gate = new object();
        private static readonly Queue<ConsoleEntry> Entries = new Queue<ConsoleEntry>();

        static ConsoleRecorder()
        {
            Application.logMessageReceivedThreaded -= OnLog;
            Application.logMessageReceivedThreaded += OnLog;
        }

        private static void OnLog(string message, string stackTrace, LogType type)
        {
            var kind = type == LogType.Error ? EntryKind.Error
                : type == LogType.Assert ? EntryKind.Assert
                : type == LogType.Exception ? EntryKind.Exception
                : type == LogType.Warning ? EntryKind.Warning
                : EntryKind.Log;
            lock (Gate)
            {
                Entries.Enqueue(new ConsoleEntry { Kind = kind, Message = message ?? "", StackTrace = stackTrace ?? "" });
                while (Entries.Count > Capacity) Entries.Dequeue();
            }
        }

        internal static List<ConsoleEntry> Snapshot()
        {
            lock (Gate) return new List<ConsoleEntry>(Entries);
        }

        internal static void Clear()
        {
            lock (Gate) Entries.Clear();
        }
    }
}
