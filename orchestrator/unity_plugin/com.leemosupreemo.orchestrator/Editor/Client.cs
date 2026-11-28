using System;
using UnityEditor;
using UnityEngine;
using UnityEngine.Networking;

namespace Orchestrator.UnityBridge
{
    internal sealed class Reply
    {
        internal long Status;
        internal string Text = "";
        /// <summary>What to tell the person, or null when the call worked.</summary>
        internal string Error;

        internal bool Ok => Error == null;
    }

    [Serializable] internal sealed class ErrorBody { public string error; }

    /// <summary>One call to the Orchestrator web API. The callback runs on the Editor's main thread.</summary>
    internal static class Client
    {
        internal static void Get(string path, Action<Reply> done) => Send("GET", path, null, done);

        internal static void Post(string path, string json, Action<Reply> done) => Send("POST", path, json, done);

        private static void Send(string method, string path, string json, Action<Reply> done)
        {
            string url;
            try
            {
                url = Connection.Endpoint(Settings.Url, path);
            }
            catch (ArgumentException exception)
            {
                done(new Reply { Error = exception.Message });
                return;
            }

            var token = Settings.EffectiveToken();
            if (token.Length == 0)
            {
                done(new Reply { Error = "No access token. Paste it in the settings, or run Orchestrator on this computer." });
                return;
            }

            var request = new UnityWebRequest(url, method) { downloadHandler = new DownloadHandlerBuffer(), timeout = 20 };
            request.SetRequestHeader("Authorization", "Bearer " + token);
            // Orchestrator only accepts changes that carry this header, so another website can't make them.
            request.SetRequestHeader("X-Orchestrator-UI", "1");
            if (json != null)
            {
                request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes(json));
                request.SetRequestHeader("Content-Type", "application/json");
            }

            var operation = request.SendWebRequest();
            EditorApplication.CallbackFunction poll = null;
            poll = () =>
            {
                if (!operation.isDone) return;
                EditorApplication.update -= poll;
                var reply = Describe(request);
                request.Dispose();
                done(reply);
            };
            EditorApplication.update += poll;
        }

        private static Reply Describe(UnityWebRequest request)
        {
            var reply = new Reply { Status = request.responseCode, Text = request.downloadHandler?.text ?? "" };
            if (request.result == UnityWebRequest.Result.ConnectionError)
            {
                reply.Error = "Couldn't reach Orchestrator at " + Settings.Url + ". Is `orchestrator ui` running?";
            }
            else if (reply.Status == 401)
            {
                reply.Error = "Orchestrator refused the access token. Paste the current one in the settings.";
            }
            else if (reply.Status < 200 || reply.Status >= 300)
            {
                reply.Error = Message(reply.Text) ?? ("Orchestrator answered " + reply.Status + ".");
            }
            return reply;
        }

        private static string Message(string body)
        {
            try
            {
                var parsed = JsonUtility.FromJson<ErrorBody>(body);
                return string.IsNullOrEmpty(parsed?.error) ? null : parsed.error;
            }
            catch (ArgumentException)
            {
                return null;
            }
        }
    }
}
