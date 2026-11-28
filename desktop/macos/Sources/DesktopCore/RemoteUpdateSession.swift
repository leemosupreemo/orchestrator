import Foundation

/// Owns the real updater cancellation hooks for one remote operation. Retain a cancelled session
/// until its Sparkle cycle ends, so late callbacks skip that operation instead of arming an installer.
@MainActor public final class RemoteUpdateSession {
    public let id: String
    public private(set) var active = true
    public private(set) var installing = false
    private var extracting = false
    public var cancellationComplete: Bool { !active && !extracting }
    private var cancelDownload: (() -> Void)?
    private var installReply: (() -> Void)?
    private var cancelReady: (() -> Void)?
    public init(id: String) { self.id = id }
    public func downloadStarted(cancel: @escaping () -> Void) {
        guard active else { cancel(); return }
        cancelDownload = cancel
    }
    public func extractionStarted() { cancelDownload = nil; extracting = true }
    public func ready(install: @escaping () -> Void, cancel: @escaping () -> Void) {
        cancelDownload = nil; extracting = false
        guard active else { cancel(); return }
        installReply = install; cancelReady = cancel
    }
    @discardableResult public func cancel() -> Bool {
        guard !installing else { return false }
        active = false
        let download = cancelDownload, ready = cancelReady
        cancelDownload = nil; cancelReady = nil; installReply = nil
        download?(); ready?()
        return true
    }
    public func terminalState(errorCode: Int?) -> String? {
        guard active, !installing else { return nil }
        return errorCode == nil || errorCode == 1001 ? "current" : "failed"
    }
    public func install() {
        guard active, let reply = installReply else { return }
        installing = true
        installReply = nil; cancelReady = nil; cancelDownload = nil
        reply()
    }
}
