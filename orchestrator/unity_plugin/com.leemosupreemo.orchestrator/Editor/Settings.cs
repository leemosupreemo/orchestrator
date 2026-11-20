using System;
using UnityEditor;

namespace Orchestrator.UnityBridge
{
    /// <summary>Per-user settings, kept in the Editor's preferences so a token never lands in the project or in version control.</summary>
    internal static class Settings
    {
        private const string UrlKey = "Orchestrator.UnityBridge.Url";
        private const string TokenKey = "Orchestrator.UnityBridge.Token";

        internal static string Url
        {
            get => EditorPrefs.GetString(UrlKey, Connection.DefaultUrl);
            set => EditorPrefs.SetString(UrlKey, value ?? "");
        }

        /// <summary>The token typed in, which wins; otherwise the one Orchestrator keeps on this computer.</summary>
        internal static string Token
        {
            get => EditorPrefs.GetString(TokenKey, "");
            set => EditorPrefs.SetString(TokenKey, (value ?? "").Trim());
        }

        internal static string EffectiveToken()
        {
            var typed = Token;
            return typed.Length > 0 ? typed : Connection.ReadLocalToken(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        }
    }
}
