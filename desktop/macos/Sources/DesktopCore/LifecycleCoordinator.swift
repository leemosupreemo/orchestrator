import Foundation

@MainActor public struct LifecycleCoordinator {
    private let client: AgentClient
    private let services: ServiceController
    private let start: () async throws -> Void
    public init(client: AgentClient, services: ServiceController, start: @escaping () async throws -> Void) {
        self.client = client; self.services = services; self.start = start
    }
    public func ensureRunning() async throws {
        if services.suspended { try await services.resumeOnLaunch() }
        do { _ = try await client.request(command: "status"); return }
        catch let error as AgentError where error.code == "unavailable" { try await start() }
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            do {
                let status = try await client.request(command: "status")
                if status.result?["local_interface"]?.string == "ready" { return }
            } catch {}
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw AgentError(code: "start_timeout", message: "The agent could not start. Check for an existing CLI server in Diagnostics.")
    }
}
