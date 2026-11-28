import Foundation

/// Browser routes the menu app opens. Query values are percent-encoded strictly (only unreserved characters stay),
/// so "+", "&" and "#" in what someone typed reach the page as typed.
public enum DesktopRoutes {
    static let unreserved = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))

    public static func encode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? ""
    }

    /// The new-project form, started from what they said they want to build.
    public static func newProject(pitch: String) -> String {
        let trimmed = String(pitch.trimmingCharacters(in: .whitespacesAndNewlines).prefix(300))
        return trimmed.isEmpty ? "#/new-project" : "#/new-project?pitch=" + encode(trimmed)
    }

    public static func setup(folder: String) -> String { "#/setup?root=" + encode(folder) }
}
