import Foundation
import Combine

@MainActor public final class DesktopState: ObservableObject {
    public typealias Request = (String, [String: JSONValue]) async throws -> AgentReply
    @Published public private(set) var status: JSONValue?
    @Published public var error = ""
    @Published public var operation = ""
    private let request: Request
    private var generation = 0
    public init(request: @escaping Request) { self.request = request }
    public var canStop: Bool {
        status?["agent"]?.string == "running" && status?["activity"]?["runs"]?.int == 0 && status?["activity"]?["tasks"]?.int == 0
    }
    /// For the update scheduler: unknown whenever the agent can't be asked, so an update never treats it as idle.
    public var activity: UpdateScheduler.Activity {
        guard status?["agent"]?.string == "running", let runs = status?["activity"]?["runs"]?.int,
              let tasks = status?["activity"]?["tasks"]?.int else { return .unknown }
        return runs + tasks == 0 ? .idle : .busy
    }
    public var statusText: String {
        guard let status else { return "Agent unavailable" }
        let local = status["local_interface"]?.string == "ready" ? "Available locally" : "Local interface starting"
        let remote = status["remote_access"]?.string ?? "unavailable"
        return "\(local) · Remote access \(remote == "waiting_for_legacy" ? "paused" : remote)"
    }
    /// What to tell the person about an Orchestrator installed earlier with `orchestrator service install`.
    public var legacyText: String? {
        switch status?["legacy"]?.string {
        case "running": return "An older Orchestrator background service is running, so remote access is paused here. When its work finishes, run `orchestrator service uninstall` in Terminal, then choose Check Again."
        case "stopped": return "An older Orchestrator background service is installed but not running. Move to the app to remove it; your projects and account connection stay as they are."
        case "foreign": return "A background service named com.orchestrator.ui exists but doesn't look like Orchestrator's. It was left untouched."
        default: return nil
        }
    }
    public var canMigrateLegacy: Bool { status?["legacy"]?.string == "stopped" }
    public var activityText: String {
        guard let runs = status?["activity"]?["runs"]?.int, let tasks = status?["activity"]?["tasks"]?.int else { return "Activity unknown" }
        return runs + tasks == 0 ? "No work in progress" : "\(runs) runs · \(tasks) tasks in progress"
    }
    public func refresh() async {
        generation += 1
        let current = generation
        do {
            let reply = try await request("status", [:])
            guard current == generation else { return }
            status = reply.result; error = ""
        } catch {
            guard current == generation else { return }
            status = nil; self.error = error.localizedDescription
        }
    }
    @discardableResult public func command(_ command: String, params: [String: JSONValue] = [:]) async throws -> JSONValue {
        let reply = try await request(command, params)
        guard let result = reply.result else { throw AgentError(code: "invalid_reply", message: "The agent returned no result.") }
        return result
    }
    public func perform(_ command: String, params: [String: JSONValue] = [:]) {
        Task {
            do { _ = try await self.command(command, params: params); await refresh() }
            catch { self.error = error.localizedDescription }
        }
    }
}
