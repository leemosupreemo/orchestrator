import Foundation

public enum DesktopPaths {
    public static var isIntegrationFixture: Bool { Bundle.main.object(forInfoDictionaryKey: "OrchestratorIntegrationFixture") as? Bool == true }
    public static var controlDirectory: String {
        if let explicit = ProcessInfo.processInfo.environment["ORCHESTRATOR_DESKTOP_CONTROL_DIR"] { return explicit }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orchestrator/" + (isIntegrationFixture ? "test" : "control")).path
    }
    public static var stateDirectory: String {
        ProcessInfo.processInfo.environment["ORCHESTRATOR_USER_STATE_DIR"] ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(isIntegrationFixture ? ".orchestrator-desktop-test" : ".orchestrator").path
    }
    public static var agentPlistName: String { Bundle.main.object(forInfoDictionaryKey: "OrchestratorAgentPlist") as? String ?? "com.orchestrator.desktop.agent.plist" }
}
