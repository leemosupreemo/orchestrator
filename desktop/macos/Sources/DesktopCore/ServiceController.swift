import Foundation
import Combine
import ServiceManagement

public enum RegistrationState: String { case enabled, requiresApproval, notRegistered, notFound }

@MainActor public struct ServiceAdapter {
    public let state: () -> RegistrationState
    public let register, unregister: () async throws -> Void
    /// Only the agent's login item. An update keeps the menu's, so the replaced app comes back at login even when
    /// Sparkle installs on quit without relaunching it.
    public let unregisterAgent: () async throws -> Void
    public let prepare: () async throws -> Bool
    public let cancel: () async -> Void
    public let stop, waitStopped, start: () async throws -> Void
    public let load: (String) -> Bool
    public let save: (String, Bool) -> Void
    public init(state: @escaping () -> RegistrationState, register: @escaping () async throws -> Void, unregister: @escaping () async throws -> Void,
                prepare: @escaping () async throws -> Bool, cancel: @escaping () async -> Void, stop: @escaping () async throws -> Void,
                waitStopped: @escaping () async throws -> Void, start: @escaping () async throws -> Void,
                load: @escaping (String) -> Bool, save: @escaping (String, Bool) -> Void,
                unregisterAgent: (() async throws -> Void)? = nil) {
        self.state = state; self.register = register; self.unregister = unregister; self.prepare = prepare; self.cancel = cancel
        self.unregisterAgent = unregisterAgent ?? unregister
        self.stop = stop; self.waitStopped = waitStopped; self.start = start; self.load = load; self.save = save
    }
    public static func live(client: AgentClient, start: @escaping () async throws -> Void) -> ServiceAdapter {
        let menu = SMAppService.mainApp
        let agent = SMAppService.agent(plistName: DesktopPaths.agentPlistName)
        return ServiceAdapter(state: {
            switch agent.status {
            case .enabled: return menu.status == .enabled ? .enabled : .notRegistered
            case .requiresApproval: return .requiresApproval
            case .notFound: return .notFound
            default: return .notRegistered
            }
        }, register: {
            if menu.status != .enabled { try menu.register() }
            if agent.status != .enabled { try agent.register() }
            // Approval may be pending: keep the local workspace available on demand.
            if agent.status != .enabled { try await start() }
        }, unregister: {
            if agent.status == .enabled || agent.status == .requiresApproval { try await agent.unregister() }
            if menu.status == .enabled || menu.status == .requiresApproval { try await menu.unregister() }
        }, prepare: {
            try await client.request(command: "prepare_update").result?["accepted"]?.bool == true
        }, cancel: { _ = try? await client.request(command: "cancel_update") }, stop: {
            do {
                let reply = try await client.request(command: "stop_if_idle")
                guard reply.result?["accepted"]?.bool == true else { throw AgentError(code: "busy", message: "Work is still running.") }
            } catch let error as AgentError where error.code == "unavailable" {
                // Unregister may already have stopped launchd's instance. waitStopped verifies disappearance.
            }
        }, waitStopped: {
            let deadline = Date().addingTimeInterval(30)
            while Date() < deadline {
                do { _ = try await client.request(command: "status") }
                catch let error as AgentError where error.code == "unavailable" {
                    if AgentInstanceLease.isAvailable(stateDirectory: DesktopPaths.stateDirectory) { return }
                }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            throw AgentError(code: "stop_timeout", message: "The old agent is still running. No replacement was started.")
        }, start: start,
        load: { UserDefaults.standard.bool(forKey: "Orchestrator.desktop." + $0) },
        save: { UserDefaults.standard.set($1, forKey: "Orchestrator.desktop." + $0) },
        unregisterAgent: { if agent.status == .enabled || agent.status == .requiresApproval { try await agent.unregister() } })
    }
}

@MainActor public final class ServiceController: ObservableObject {
    @Published public private(set) var desired: Bool
    @Published public private(set) var pending = false
    @Published public private(set) var suspended: Bool
    @Published public private(set) var registration: RegistrationState
    private let bundle: URL
    private let adapter: ServiceAdapter
    private var changing = false
    public init(bundle: URL, adapter: ServiceAdapter) {
        self.bundle = bundle; self.adapter = adapter
        desired = adapter.load("startAtLogin"); suspended = adapter.load("suspended")
        registration = adapter.state()
    }
    public func registrationState() -> RegistrationState { adapter.state() }
    public static func installedInApplications(_ url: URL) -> Bool {
        let parent = url.standardizedFileURL.deletingLastPathComponent().path
        let userApplications = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications").path
        return url.pathExtension == "app" && (parent == "/Applications" || parent == userApplications)
    }
    public func setStartAtLogin(_ enabled: Bool) async throws {
        guard Self.installedInApplications(bundle) else {
            throw AgentError(code: "install_required", message: "Drag Orchestrator to Applications and launch it there before enabling Start at login.")
        }
        desired = enabled; adapter.save("startAtLogin", enabled)
        suspended = false; adapter.save("suspended", false)
        pending = true
        try await reconcile()
    }
    public func reconcile() async throws {
        registration = adapter.state()
        guard pending, !suspended, !changing else { return }
        changing = true
        defer { changing = false; registration = adapter.state() }
        guard try await adapter.prepare() else { return }
        let oldEnabled = registration == .enabled || registration == .requiresApproval
        do {
            try await adapter.unregister()
            try await adapter.stop()
            try await adapter.waitStopped()
            if desired { try await adapter.register() } else { try await adapter.start() }
            pending = false
        } catch {
            await adapter.cancel()
            if oldEnabled { try? await adapter.register() } else { try? await adapter.start() }
            throw error
        }
    }
    public func resumeOnLaunch() async throws {
        guard suspended else { return }
        suspended = false; adapter.save("suspended", false)
        if desired { try await adapter.register() } else { try await adapter.start() }
        registration = adapter.state()
    }
    public func stopIfIdle() async throws {
        guard !changing else { throw AgentError(code: "busy", message: "A background mode change is in progress.") }
        changing = true
        defer { changing = false; registration = adapter.state() }
        guard try await adapter.prepare() else { throw AgentError(code: "busy", message: "Work is still running. Stop will not interrupt it.") }
        let oldEnabled = registration == .enabled || registration == .requiresApproval
        do {
            try await adapter.unregister(); try await adapter.stop(); try await adapter.waitStopped()
            suspended = true; pending = false; adapter.save("suspended", true)
        } catch {
            await adapter.cancel()
            if oldEnabled { try? await adapter.register() }
            throw error
        }
    }
    public func removeBackgroundComponents() async throws {
        try await stopIfIdle()
        desired = false; adapter.save("startAtLogin", false)
    }
    public func stopPreparedForUpdate() async throws {
        guard !changing else { throw AgentError(code: "busy", message: "A background mode change is in progress.") }
        changing = true
        defer { changing = false; registration = adapter.state() }
        try await adapter.unregisterAgent(); try await adapter.stop(); try await adapter.waitStopped()
        suspended = true; pending = false; adapter.save("suspended", true)
    }
    public func restoreAfterUpdate(startAtLogin: Bool) async throws {
        desired = startAtLogin; adapter.save("startAtLogin", startAtLogin)
        suspended = false; adapter.save("suspended", false)
        if startAtLogin { try await adapter.register() } else { try await adapter.start() }
        registration = adapter.state()
    }
    /// For a Mac set up over SSH (`--register-background`): make sure the agent runs, then register the menu and agent
    /// login items without showing a window. Busy work makes the change wait, as from Settings.
    public func registerInBackground(ensureRunning: () async throws -> Void) async throws -> RegistrationState {
        try await ensureRunning()
        registration = adapter.state()
        if !(desired && registration == .enabled) { try await setStartAtLogin(true) }
        return registration
    }
    /// The login-item state, for `orchestrator enroll` and the agent's readiness checks, which can't ask macOS themselves.
    public static func recordRegistration(_ state: RegistrationState, pending: Bool, in directory: String, now: Date = Date()) {
        let record: [String: Any] = ["registration": state.rawValue, "pending": pending, "at": now.timeIntervalSince1970]
        guard let data = try? JSONSerialization.data(withJSONObject: record) else { return }
        let url = URL(fileURLWithPath: directory).appendingPathComponent("background.json")
        try? data.write(to: url, options: .atomic)
        _ = chmod(url.path, 0o600)
    }
    public func openApprovalSettings() { SMAppService.openSystemSettingsLoginItems() }
}
