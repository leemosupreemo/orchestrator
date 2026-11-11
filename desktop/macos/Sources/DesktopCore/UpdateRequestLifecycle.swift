import Foundation

/// Tracks which request owns the menu's busy flag, independently of retained terminal agent status.
@MainActor public final class UpdateRequestLifecycle {
    public struct StartupReport { public let state: String; public let operationID: String? }
    public static let terminalStates = ["current", "cancelled", "failed", "unavailable", "expired"]
    public private(set) var handling = false
    public private(set) var handledOperationID: String?
    private var activeRemoteID: String?
    public init() {}
    public func beginRemote(_ id: String) { handledOperationID = id; activeRemoteID = id; handling = true }
    public func beginManual() { activeRemoteID = nil; handling = true }
    public func finishManual() { activeRemoteID = nil; handling = false }
    public func observeTerminal(id: String, state: String) {
        guard Self.terminalStates.contains(state) else { return }
        if activeRemoteID == id { activeRemoteID = nil; handling = false }
        handledOperationID = id
    }
    /// Run only during launch, before this process starts any installation. An orphan claim has no
    /// completion evidence; fail it instead of declaring success or leaving it installing forever.
    public static func startupReport(operation: JSONValue?, recovered: Bool, recoveredOperationID: String?) -> StartupReport? {
        if let id = operation?["id"]?.string, operation?["state"]?.string == "updating", id != recoveredOperationID {
            return StartupReport(state: "failed", operationID: id)
        }
        guard recovered else { return nil }
        return StartupReport(state: "current", operationID: recoveredOperationID)
    }
}
