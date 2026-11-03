using System;
using UnityEditor;
using UnityEngine;

namespace Orchestrator.UnityBridge
{
    [Serializable] internal sealed class StateReply { public ProjectInfo project; }
    [Serializable] internal sealed class ProjectInfo { public string name; public string root; }
    [Serializable] internal sealed class JobsReply { public JobRow[] jobs; }
    [Serializable] internal sealed class JobRow { public string id; public string title; public string kind; public JobState state; }
    [Serializable] internal sealed class JobState { public string label; public string tone; public string group; }

    /// <summary>Window > Orchestrator: report what's broken, and see where the jobs stand.</summary>
    internal sealed class OrchestratorWindow : EditorWindow
    {
        private const double RefreshSeconds = 15;
        private const int MaxJobs = 12;
        private const int ConsoleChars = 30000;
        private const int TestChars = 30000;

        [SerializeField] private string summary = "";
        [SerializeField] private string repro = "";
        [SerializeField] private string expected = "";
        [SerializeField] private bool includeConsole = true;
        [SerializeField] private bool includeTests = true;
        [SerializeField] private bool startNow;
        [SerializeField] private Vector2 scroll;

        private bool showConnection;
        private string url;
        private string token;
        private string connectionNote = "Connecting...";
        private bool connected;
        private string project = "";
        private JobRow[] jobs = new JobRow[0];
        private string notice = "";
        private bool sending;
        private double nextRefresh;

        [MenuItem("Window/Orchestrator")]
        internal static void Open()
        {
            var window = GetWindow<OrchestratorWindow>("Orchestrator");
            window.minSize = new Vector2(340, 420);
            window.Show();
        }

        [MenuItem("Tools/Orchestrator/Send Console Problems as Bug")]
        internal static void SendConsoleProblems()
        {
            var window = GetWindow<OrchestratorWindow>("Orchestrator");
            window.includeConsole = true;
            if (string.IsNullOrWhiteSpace(window.summary))
            {
                var first = ConsoleDigest.Problems(ConsoleRecorder.Snapshot());
                window.summary = first.Count > 0 ? FirstLine(first[first.Count - 1].Message) : "";
            }
            window.Show();
        }

        private void OnEnable()
        {
            url = Settings.Url;
            token = Settings.Token;
            Refresh();
        }

        private void OnInspectorUpdate()
        {
            if (EditorApplication.timeSinceStartup >= nextRefresh) Refresh();
            Repaint();
        }

        private void Refresh()
        {
            nextRefresh = EditorApplication.timeSinceStartup + RefreshSeconds;
            Client.Get("api/state", reply =>
            {
                connected = reply.Ok;
                if (!reply.Ok)
                {
                    connectionNote = reply.Error;
                    jobs = new JobRow[0];
                    return;
                }
                var state = JsonUtility.FromJson<StateReply>(reply.Text);
                project = state?.project?.name ?? "";
                connectionNote = "Connected" + (project.Length > 0 ? " to " + project : "");
                Client.Get("api/jobs", list =>
                {
                    if (!list.Ok) return;
                    var parsed = JsonUtility.FromJson<JobsReply>(list.Text);
                    var all = parsed?.jobs ?? new JobRow[0];
                    jobs = new JobRow[Math.Min(all.Length, MaxJobs)];
                    Array.Copy(all, jobs, jobs.Length);
                });
            });
        }

        private void OnGUI()
        {
            scroll = EditorGUILayout.BeginScrollView(scroll);
            DrawConnection();
            EditorGUILayout.Space();
            DrawReport();
            EditorGUILayout.Space();
            DrawJobs();
            EditorGUILayout.EndScrollView();
        }

        private void DrawConnection()
        {
            EditorGUILayout.BeginHorizontal();
            EditorGUILayout.LabelField(connectionNote, connected ? EditorStyles.boldLabel : EditorStyles.wordWrappedLabel);
            if (GUILayout.Button("Refresh", GUILayout.Width(70))) Refresh();
            EditorGUILayout.EndHorizontal();

            showConnection = EditorGUILayout.Foldout(showConnection, "Connection", true);
            if (!showConnection) return;
            url = EditorGUILayout.TextField("Address", url);
            token = EditorGUILayout.PasswordField("Access token", token);
            EditorGUILayout.HelpBox("Leave the token empty when Orchestrator runs on this computer; it is read from ~/.orchestrator/ui_token. "
                + "The token is stored in this Editor's preferences, not in the project.", MessageType.None);
            if (GUILayout.Button("Save and reconnect"))
            {
                Settings.Url = url;
                Settings.Token = token;
                Refresh();
            }
        }

        private void DrawReport()
        {
            EditorGUILayout.LabelField("Report a problem", EditorStyles.boldLabel);
            summary = EditorGUILayout.TextField("What's wrong?", summary);
            EditorGUILayout.LabelField("What happens (optional)");
            repro = EditorGUILayout.TextArea(repro, GUILayout.MinHeight(48));
            expected = EditorGUILayout.TextField("What should happen", expected);

            var problems = ConsoleDigest.Problems(ConsoleRecorder.Snapshot()).Count;
            includeConsole = EditorGUILayout.ToggleLeft("Include console errors (" + problems + ")", includeConsole);
            using (new EditorGUI.DisabledScope(TestFailures.Count == 0))
            {
                includeTests = EditorGUILayout.ToggleLeft("Include failing tests (" + TestFailures.Count + ")", includeTests);
            }
            startNow = EditorGUILayout.ToggleLeft("Start building right away (otherwise it waits for you in Orchestrator)", startNow);

            using (new EditorGUI.DisabledScope(sending || !connected || string.IsNullOrWhiteSpace(summary)))
            {
                if (GUILayout.Button(sending ? "Sending..." : "Send to Orchestrator", GUILayout.Height(26))) Send();
            }
            if (notice.Length > 0) EditorGUILayout.HelpBox(notice, MessageType.Info);
        }

        private void Send()
        {
            var details = "";
            if (includeConsole) details += ConsoleDigest.Build(ConsoleRecorder.Snapshot(), ConsoleChars);
            if (includeTests) details += (details.Length > 0 ? "\n\n" : "") + TestFailures.Build(TestChars);
            details += (details.Length > 0 ? "\n\n" : "") + "Reported from the Unity Editor " + Application.unityVersion + ".";

            sending = true;
            notice = "";
            Client.Post("api/runs", JobRequest.NewBug(summary, repro, expected, details, startNow), reply =>
            {
                sending = false;
                if (!reply.Ok)
                {
                    notice = reply.Error;
                    return;
                }
                notice = "Sent. The job shows up below once Orchestrator has planned it.";
                summary = repro = expected = "";
                nextRefresh = EditorApplication.timeSinceStartup + 4;
            });
        }

        private void DrawJobs()
        {
            EditorGUILayout.LabelField("Jobs", EditorStyles.boldLabel);
            if (jobs.Length == 0)
            {
                EditorGUILayout.LabelField(connected ? "No jobs yet." : "", EditorStyles.miniLabel);
                return;
            }
            foreach (var job in jobs)
            {
                EditorGUILayout.BeginHorizontal();
                EditorGUILayout.LabelField(new GUIContent(job.title, job.title), GUILayout.MinWidth(120));
                EditorGUILayout.LabelField(job.state?.label ?? "", EditorStyles.miniLabel, GUILayout.Width(110));
                if (GUILayout.Button("Open", GUILayout.Width(50)))
                {
                    try { Application.OpenURL(Connection.JobPage(Settings.Url, job.id)); }
                    catch (ArgumentException exception) { notice = exception.Message; }
                }
                EditorGUILayout.EndHorizontal();
            }
        }

        private static string FirstLine(string text)
        {
            var line = (text ?? "").Trim().Split('\n')[0].Trim();
            return line.Length <= 200 ? line : line.Substring(0, 200);
        }
    }
}
