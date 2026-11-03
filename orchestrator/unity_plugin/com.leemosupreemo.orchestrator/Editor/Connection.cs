using System;
using System.IO;

namespace Orchestrator.UnityBridge
{
    /// <summary>Where the running Orchestrator is, and the token that lets the Editor talk to it.</summary>
    internal static class Connection
    {
        internal const string DefaultUrl = "http://127.0.0.1:8765";

        /// <summary>A clean base address. Plain http is only for this computer: the token would travel in the clear otherwise.</summary>
        internal static string NormalizeUrl(string url)
        {
            url = (url ?? "").Trim().TrimEnd('/');
            if (url.Length == 0) url = DefaultUrl;
            if (!url.Contains("://")) url = "https://" + url;
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || (uri.Scheme != "http" && uri.Scheme != "https"))
                throw new ArgumentException("Use an http or https address, like " + DefaultUrl + ".");
            if (!string.IsNullOrEmpty(uri.UserInfo))
                throw new ArgumentException("Leave the user name and password out of the address.");
            if (uri.Scheme == "http" && !uri.IsLoopback)
                throw new ArgumentException("Use https for another computer, so the access token isn't sent in the clear.");
            return uri.GetLeftPart(UriPartial.Authority) + (uri.AbsolutePath == "/" ? "" : uri.AbsolutePath.TrimEnd('/'));
        }

        internal static string Endpoint(string baseUrl, string path)
        {
            return NormalizeUrl(baseUrl) + "/" + path.TrimStart('/');
        }

        /// <summary>The token `orchestrator ui` keeps for this computer, or "" when it isn't there (Orchestrator runs elsewhere).</summary>
        internal static string ReadLocalToken(string home)
        {
            try
            {
                var file = Path.Combine(home ?? "", ".orchestrator", "ui_token");
                return File.Exists(file) ? File.ReadAllText(file).Trim() : "";
            }
            catch (Exception)
            {
                return "";
            }
        }

        /// <summary>The page for one job in the Orchestrator web app.</summary>
        internal static string JobPage(string baseUrl, string jobId)
        {
            return NormalizeUrl(baseUrl) + "/#/jobs/" + Uri.EscapeDataString(jobId ?? "");
        }
    }
}
