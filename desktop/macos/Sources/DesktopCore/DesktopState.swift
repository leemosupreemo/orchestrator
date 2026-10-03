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
    public var statusText: String {
        guard let status else { return "Agent unavailable" }
        let local = status["local_interface"]?.string == "ready" ? "Available locally" : "Local interface starting"
        return "\(local) · Remote access \(status["remote_access"]?.string ?? "unavailable")"
    }
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
