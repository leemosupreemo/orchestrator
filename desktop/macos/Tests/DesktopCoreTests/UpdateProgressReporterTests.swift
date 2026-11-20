import XCTest
@testable import DesktopCore

@MainActor final class UpdateProgressReporterTests: XCTestCase {
    func testRejectedTerminalProgressRetriesUntilAcknowledged() async {
        var attempts = 0, reports: [String] = []
        let reporter = UpdateProgressReporter { progress, operationID in
            reports.append((operationID ?? "manual") + ":" + progress)
            attempts += 1
            return attempts > 1
        }
        reporter.enqueue("cancelled", operationID: "operation")
        await reporter.flush(); await reporter.flush(); await reporter.flush()
        XCTAssertEqual(reports, ["operation:cancelled", "operation:cancelled"])
    }
    func testLaterProgressSurvivesEarlierInFlightAcknowledgement() async {
        var resume: CheckedContinuation<Bool, Never>?
        var reports: [String] = []
        let reporter = UpdateProgressReporter { progress, _ in
            reports.append(progress)
            if progress == "checking" { return await withCheckedContinuation { resume = $0 } }
            return true
        }
        reporter.enqueue("checking", operationID: "operation")
        let old = Task { await reporter.flush() }
        while resume == nil { await Task.yield() }
        reporter.enqueue("cancelled", operationID: "operation")
        resume?.resume(returning: true)
        await old.value
        await reporter.flush()
        XCTAssertEqual(reports, ["checking", "cancelled"])
    }
    func testNewOperationIsNotDeduplicatedAgainstOldOperation() async {
        var reports: [String] = []
        let reporter = UpdateProgressReporter { progress, operationID in
            reports.append((operationID ?? "manual") + ":" + progress)
            return true
        }
        reporter.enqueue("checking", operationID: "old"); await reporter.flush()
        reporter.enqueue("checking", operationID: "new"); await reporter.flush()
        reporter.enqueue("checking", operationID: "new"); await reporter.flush()
        XCTAssertEqual(reports, ["old:checking", "new:checking"])
    }
    func testTransportFailureRetainsProgressForNextRefresh() async {
        var attempts = 0
        let reporter = UpdateProgressReporter { _, _ in
            attempts += 1
            if attempts == 1 { throw AgentError(code: "unavailable", message: "Agent disconnected") }
            return true
        }
        reporter.enqueue("current", operationID: "operation")
        await reporter.flush(); await reporter.flush(); await reporter.flush()
        XCTAssertEqual(attempts, 2)
    }

}
