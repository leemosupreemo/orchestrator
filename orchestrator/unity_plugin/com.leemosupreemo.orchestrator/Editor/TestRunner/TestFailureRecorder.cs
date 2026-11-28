using UnityEditor;
using UnityEditor.TestTools.TestRunner.Api;
using UnityEngine;

namespace Orchestrator.UnityBridge
{
    /// <summary>Feeds failing tests from the Test Runner into <see cref="TestFailures"/>.</summary>
    [InitializeOnLoad]
    internal static class TestFailureRecorder
    {
        private static TestRunnerApi api;

        static TestFailureRecorder()
        {
            api = ScriptableObject.CreateInstance<TestRunnerApi>();
            api.RegisterCallbacks(new Callbacks());
        }

        private sealed class Callbacks : ICallbacks
        {
            public void RunStarted(ITestAdaptor testsToRun) => TestFailures.Clear();

            public void RunFinished(ITestResultAdaptor result) { }

            public void TestStarted(ITestAdaptor test) { }

            public void TestFinished(ITestResultAdaptor result)
            {
                // Only leaf tests: a failed fixture would repeat what its tests already say.
                if (result.HasChildren || result.TestStatus != TestStatus.Failed) return;
                TestFailures.Add(result.Test.FullName, result.Message, result.StackTrace);
            }
        }
    }
}
