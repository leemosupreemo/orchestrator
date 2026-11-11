import Foundation

/// Coalesces progress while preserving an unacknowledged final result for the next status refresh.
@MainActor public final class UpdateProgressReporter {
    private struct Report: Equatable { let state: String; let operationID: String? }
    private var pending: Report?
    private var acknowledged: Report?
    private var sending = false
    private let send: (String, String?) async throws -> Bool
    public init(send: @escaping (String, String?) async throws -> Bool) { self.send = send }
    public func enqueue(_ state: String, operationID: String? = nil) {
        let report = Report(state: state, operationID: operationID)
        guard report != acknowledged else { return }
        pending = report
    }
    public func flush() async {
        guard !sending, let report = pending else { return }
        sending = true
        defer { sending = false }
        guard (try? await send(report.state, report.operationID)) == true else { return }
        acknowledged = report
        if pending == report { pending = nil }
    }
}
