import XCTest
@testable import DesktopCore

@MainActor final class UpdateRequestLifecycleTests: XCTestCase {
    private func operation(_ state: String, id: String = "operation") -> JSONValue {
        .object(["id": .string(id), "state": .string(state)])
    }
    func testStartupOrphanInstallationReportsFailureForExactOperation() {
        let report = UpdateRequestLifecycle.startupReport(operation: operation("updating"), recovered: false, recoveredOperationID: nil)
        XCTAssertEqual(report?.state, "failed")
        XCTAssertEqual(report?.operationID, "operation")
    }
    func testStartupRequiresMatchingCompletionEvidence() {
        let report = UpdateRequestLifecycle.startupReport(operation: operation("updating"), recovered: true, recoveredOperationID: "old-operation")
        XCTAssertEqual(report?.state, "failed")
        XCTAssertEqual(report?.operationID, "operation")
        let matched = UpdateRequestLifecycle.startupReport(operation: operation("updating"), recovered: true, recoveredOperationID: "operation")
        XCTAssertEqual(matched?.state, "current")
        XCTAssertEqual(matched?.operationID, "operation")
    }
    func testStartupLeavesUnclaimedOperationAndFreshLocalLaunchAlone() {
        XCTAssertNil(UpdateRequestLifecycle.startupReport(operation: operation("checking"), recovered: false, recoveredOperationID: nil))
        XCTAssertNil(UpdateRequestLifecycle.startupReport(operation: nil, recovered: false, recoveredOperationID: nil))
        let local = UpdateRequestLifecycle.startupReport(operation: nil, recovered: true, recoveredOperationID: nil)
        XCTAssertEqual(local?.state, "current")
        XCTAssertNil(local?.operationID)
    }
    func testRemoteCurrentReleasesHandlingForLaterLegacyRequest() {
        let lifecycle = UpdateRequestLifecycle()
        lifecycle.beginRemote("operation")
        XCTAssertTrue(lifecycle.handling)
        lifecycle.observeTerminal(id: "operation", state: "current")
        XCTAssertFalse(lifecycle.handling)
        lifecycle.beginManual()
        // Retained old remote terminal state must not clear the new manual request on each refresh.
        lifecycle.observeTerminal(id: "operation", state: "current")
        XCTAssertTrue(lifecycle.handling)
        lifecycle.finishManual()
        XCTAssertFalse(lifecycle.handling)
    }
    func testUnrelatedTerminalDoesNotFinishActiveRemoteRequest() {
        let lifecycle = UpdateRequestLifecycle()
        lifecycle.beginRemote("new")
        lifecycle.observeTerminal(id: "old", state: "failed")
        XCTAssertTrue(lifecycle.handling)
        lifecycle.observeTerminal(id: "new", state: "checking")
        XCTAssertTrue(lifecycle.handling)
        lifecycle.observeTerminal(id: "new", state: "cancelled")
        XCTAssertFalse(lifecycle.handling)
    }
}
