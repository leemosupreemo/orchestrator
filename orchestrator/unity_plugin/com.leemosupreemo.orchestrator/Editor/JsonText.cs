using System.Text;

namespace Orchestrator.UnityBridge
{
    /// <summary>Just enough JSON to talk to the Orchestrator API without depending on a JSON package.</summary>
    internal static class JsonText
    {
        internal static string Quote(string value)
        {
            var text = new StringBuilder((value ?? "").Length + 2);
            text.Append('"');
            foreach (var c in value ?? "")
            {
                switch (c)
                {
                    case '"': text.Append("\\\""); break;
                    case '\\': text.Append("\\\\"); break;
                    case '\n': text.Append("\\n"); break;
                    case '\r': text.Append("\\r"); break;
                    case '\t': text.Append("\\t"); break;
                    default:
                        if (c < ' ') text.Append("\\u").Append(((int)c).ToString("x4"));
                        else text.Append(c);
                        break;
                }
            }
            return text.Append('"').ToString();
        }

        internal static string Clip(string value, int limit)
        {
            value = (value ?? "").Trim();
            return value.Length <= limit ? value : value.Substring(0, limit);
        }
    }
}
