import Foundation
import Combine

/// Installs a silently downloaded update once the agent has been idle long enough, for Macs nobody is watching.
/// It never bypasses the work gate: installation goes through `UpdateCoordinator.prepareInstallation()`, which refuses
/// while work is running. A request from the hosted app skips only the idle wait.
@MainActor public final class UpdateScheduler: ObservableObject {
    public enum Activity: Equatable { case idle, busy, unknown }
    public static let checkInterval: TimeInterval = 6 * 60 * 60
    public static let idleRequired: TimeInterval = 10 * 60
    @Published public private(set) var enabled: Bool
    @Published public private(set) var waiting = false
    public private(set) var requested = false
    private let now: () -> Date
    private let save: (Bool) -> Void
    private let prepare: () async throws -> Void
    private var idleSince: Date?
    private var install: (() -> Void)?
    private var installing = false
    public init(enabled: Bool, now: @escaping () -> Date = Date.init, save: @escaping (Bool) -> Void, prepare: @escaping () async throws -> Void) {
        self.enabled = enabled; self.now = now; self.save = save; self.prepare = prepare
    }
    public func setEnabled(_ value: Bool) { enabled = value; save(value) }
    /// Sparkle downloaded an update and handed over the block that installs it and relaunches the app.
    public func updateDownloaded(install: @escaping () -> Void) { self.install = install; waiting = true }
    /// Install as soon as no work is running, without the idle wait.
    public func request() { requested = true }
    public func cancel() { install = nil; waiting = false; requested = false }
    /// Called on every status refresh. Returns true when installation was handed to Sparkle.
    @discardableResult public func observe(_ activity: Activity) async -> Bool {
        if activity == .idle { idleSince = idleSince ?? now() } else { idleSince = nil }
        guard let install, !installing, enabled || requested, activity == .idle, let idleSince else { return false }
        guard requested || now().timeIntervalSince(idleSince) >= Self.idleRequired else { return false }
        installing = true
        defer { installing = false }
        do { try await prepare() } catch { self.idleSince = nil; return false }
        self.install = nil; waiting = false; requested = false
        install()
        return true
    }
}
