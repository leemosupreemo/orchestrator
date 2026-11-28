using System.Text;

namespace Orchestrator.UnityBridge
{
    /// <summary>The body of a "new job" call, shaped like the one the Orchestrator web page sends.</summary>
    internal static class JobRequest
    {
        // The server refuses longer text, so cut here rather than lose the whole request.
        internal const int SummaryLimit = 500;
        internal const int FieldLimit = 20000;
        internal const int DetailsLimit = 200000;

        /// <param name="startNow">False plans the job and waits for a person to start it in Orchestrator.</param>
        internal static string NewBug(string summary, string repro, string expected, string details, bool startNow)
        {
            var body = new StringBuilder();
            body.Append("{\"action\":\"new_job\",\"params\":{\"type\":\"bug\"");
            body.Append(",\"summary\":").Append(JsonText.Quote(JsonText.Clip(summary, SummaryLimit)));
            body.Append(",\"repro\":").Append(JsonText.Quote(JsonText.Clip(repro, FieldLimit)));
            body.Append(",\"expected\":").Append(JsonText.Quote(JsonText.Clip(expected, FieldLimit)));
            body.Append(",\"spec\":").Append(JsonText.Quote(JsonText.Clip(details, DetailsLimit)));
            body.Append(",\"no_dispatch\":").Append(startNow ? "false" : "true");
            return body.Append("}}").ToString();
        }
    }
}
