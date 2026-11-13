import XCTest
@testable import DesktopCore

@MainActor final class UpdateCoordinatorTests: XCTestCase {
    @MainActor final class Fixture {
        var busy = false, unavailable = false, fail = false, restoreFails = false
        var journal: UpdateJournal?
        var events: [String] = []
        func coordinator() -> UpdateCoordinator {
            UpdateCoordinator(adapter: UpdateAdapter(prepare: {
                if self.unavailable { throw AgentError(code: "unavailable", message: "Unknown agent activity") }
                return !self.busy
            }, cancel: { self.events.append("cancel") }, stop: {
                self.events.append("stop")
                if self.fail { throw AgentError(code: "stop_failed", message: "Cannot stop") }
            }, restore: { enabled in
                self.events.append(enabled ? "restore_login" : "restore_local")
                if self.restoreFails { throw AgentError(code: "restore_failed", message: "Restore failed") }
            }, desired: { true }, load: { self.journal }, save: { self.journal = $0 }))
        }
    }
    func testBusyInstallationDoesNotStopOrPersistJournal() async {
        let fixture = Fixture(); fixture.busy = true
        let coordinator = fixture.coordinator()
        do { try await coordinator.prepareInstallation(); XCTFail("busy install") } catch {}
        XCTAssertEqual(coordinator.state, .waiting)
        XCTAssertTrue(fixture.events.isEmpty)
        XCTAssertNil(fixture.journal)
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
    }
    func testUnknownAgentNeverCountsAsIdle() async {
        let fixture = Fixture(); fixture.unavailable = true
        let coordinator = fixture.coordinator()
        do { try await coordinator.prepareInstallation(); XCTFail("unknown install") } catch {}
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
        XCTAssertTrue(fixture.events.isEmpty)
    }
    func testAcceptedInstallationStopsThenAllowsTermination() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        try await coordinator.prepareInstallation()
        XCTAssertEqual(fixture.events, ["stop"])
        XCTAssertTrue(coordinator.mayTerminateForInstallation)
        XCTAssertEqual(fixture.journal?.startAtLogin, true)
    }
    func testFailureReleasesGateAndRestoresMode() async {
        let fixture = Fixture(); fixture.fail = true
        let coordinator = fixture.coordinator()
        do { try await coordinator.prepareInstallation(); XCTFail("failed stop") } catch {}
        XCTAssertEqual(fixture.events, ["stop", "cancel", "restore_login"])
        XCTAssertNil(fixture.journal)
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
    }
    func testCancelledInstallationRestoresPreviousAgent() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        try await coordinator.prepareInstallation()
        await coordinator.cancelInstallation()
        XCTAssertEqual(fixture.events, ["stop", "cancel", "restore_login"])
        XCTAssertNil(fixture.journal)
    }
    func testCancelWithoutPreparedInstallationLeavesAgentGateAlone() async {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        await coordinator.cancelInstallation()
        XCTAssertTrue(fixture.events.isEmpty)
        XCTAssertEqual(coordinator.state, .current)
    }
    func testRelaunchRecoversOnlyTypedNonsecretJournal() async throws {
        let fixture = Fixture(); fixture.journal = UpdateJournal(startAtLogin: false, identity: "0.2.0")
        let coordinator = fixture.coordinator()
        try await coordinator.recoverAfterRelaunch()
        XCTAssertEqual(fixture.events, ["restore_local"])
        XCTAssertNil(fixture.journal)
    }
    func testRemoteOperationIdentitySurvivesInstallationRelaunch() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        coordinator.operationID = "recovery-1"
        try await coordinator.prepareInstallation()
        XCTAssertEqual(fixture.journal?.operationID, "recovery-1")
        let relaunched = fixture.coordinator()
        let recovered = try await relaunched.recoverAfterRelaunch()
        XCTAssertTrue(recovered)
        XCTAssertEqual(relaunched.recoveredOperationID, "recovery-1")
        XCTAssertEqual(fixture.journal?.operationID, "recovery-1")
        try relaunched.acknowledgeRecovery(operationID: "unrelated")
        XCTAssertNotNil(fixture.journal)
        try relaunched.acknowledgeRecovery(operationID: "recovery-1")
        XCTAssertNil(fixture.journal)
    }

    func testAlreadyPreparedInstallationDoesNotStopServicesTwice() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        try await coordinator.prepareInstallation()
        try await coordinator.prepareInstallation()
        XCTAssertEqual(fixture.events, ["stop"])
    }
    func testConcurrentPreparationIsRefusedWhileFirstClaimIsPending() async throws {
        var resume: CheckedContinuation<Bool, Never>?
        var stops = 0
        let coordinator = UpdateCoordinator(adapter: UpdateAdapter(prepare: {
            await withCheckedContinuation { resume = $0 }
        }, cancel: {}, stop: { stops += 1 }, restore: { _ in }, desired: { false }, load: { nil }, save: { _ in }))
        let first = Task { try await coordinator.prepareInstallation() }
        while resume == nil { await Task.yield() }
        do { try await coordinator.prepareInstallation(); XCTFail("concurrent claim allowed") }
        catch let error as AgentError { XCTAssertEqual(error.code, "busy") }
        resume?.resume(returning: true)
        try await first.value
        XCTAssertEqual(stops, 1)
    }
    func testFailedRestorationKeepsJournalForLaterRecovery() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        try await coordinator.prepareInstallation()
        fixture.restoreFails = true
        await coordinator.cancelInstallation()
        XCTAssertEqual(coordinator.state, .failed)
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
        XCTAssertNotNil(fixture.journal)
        fixture.restoreFails = false
        let recovered = try await fixture.coordinator().recoverAfterRelaunch()
        XCTAssertTrue(recovered)
        XCTAssertNil(fixture.journal)
    }
    func testFailedInstallAndFailedRestoreKeepRecoveryJournal() async {
        let fixture = Fixture(); fixture.fail = true; fixture.restoreFails = true
        let coordinator = fixture.coordinator()
        do { try await coordinator.prepareInstallation(); XCTFail("install succeeded") } catch {}
        XCTAssertEqual(coordinator.state, .failed)
        XCTAssertNotNil(fixture.journal)
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
    }
    func testMissingJournalAfterPreparationStillReleasesOwnedGate() async throws {
        let fixture = Fixture(), coordinator = fixture.coordinator()
        try await coordinator.prepareInstallation()
        fixture.journal = nil
        await coordinator.cancelInstallation()
        XCTAssertEqual(fixture.events, ["stop", "cancel"])
        XCTAssertFalse(coordinator.mayTerminateForInstallation)
    }
    func testNoJournalDoesNotReportSuccessfulRecovery() async throws {
        let fixture = Fixture()
        let recovered = try await fixture.coordinator().recoverAfterRelaunch()
        XCTAssertFalse(recovered)
        XCTAssertTrue(fixture.events.isEmpty)
    }
    func testInvalidJournalCannotRestartServices() async throws {
        let fixture = Fixture()
        fixture.journal = try JSONDecoder().decode(UpdateJournal.self, from: Data(
            #"{"schema":2,"startAtLogin":false,"identity":"0.2.0","phase":"installing"}"#.utf8))
        do { _ = try await fixture.coordinator().recoverAfterRelaunch(); XCTFail("invalid recovery accepted") }
        catch let error as AgentError { XCTAssertEqual(error.code, "invalid_journal") }
        XCTAssertTrue(fixture.events.isEmpty)
        XCTAssertNotNil(fixture.journal)
    }
    func testLegacyJournalWithoutOperationIDStillRecoversLocalInstallation() async throws {
        let fixture = Fixture()
        fixture.journal = try JSONDecoder().decode(UpdateJournal.self, from: Data(
            #"{"schema":1,"startAtLogin":false,"identity":"0.2.0","phase":"installing"}"#.utf8))
        let coordinator = fixture.coordinator()
        let recovered = try await coordinator.recoverAfterRelaunch()
        XCTAssertTrue(recovered)
        XCTAssertNil(coordinator.recoveredOperationID)
        XCTAssertEqual(fixture.events, ["restore_local"])
    }
    func testLivePreparationSendsBoundOperationIDAndLocalPreparationOmitsIt() async throws {
        let services = ServiceControllerTests.Fixture().controller()
        let remoteClient = AgentClient(transport: { data in
            let request = try JSONDecoder().decode(AgentRequest.self, from: data)
            let valid = request.command == "prepare_update" && request.params == ["operation_id": .string("recovery-1")]
            return try JSONSerialization.data(withJSONObject: ["version": 1, "request_id": request.requestID, "ok": true, "result": ["accepted": valid]])
        })
        let remote = UpdateAdapter.live(client: remoteClient, services: services, operationID: { "recovery-1" })
        let remoteAccepted = try await remote.prepare()
        XCTAssertTrue(remoteAccepted)
        let localClient = AgentClient(transport: { data in
            let request = try JSONDecoder().decode(AgentRequest.self, from: data)
            let valid = request.command == "prepare_update" && request.params.isEmpty
            return try JSONSerialization.data(withJSONObject: ["version": 1, "request_id": request.requestID, "ok": true, "result": ["accepted": valid]])
        })
        let localAccepted = try await UpdateAdapter.live(client: localClient, services: services).prepare()
        XCTAssertTrue(localAccepted)
    }

    func testUnrelatedAbortCannotEraseUnacknowledgedRecoveryEvidence() async throws {
        let fixture = Fixture()
        fixture.journal = UpdateJournal(startAtLogin: false, identity: "0.2.0", operationID: "operation")
        let coordinator = fixture.coordinator()
        _ = try await coordinator.recoverAfterRelaunch()
        await coordinator.cancelInstallation()
        XCTAssertEqual(fixture.events, ["restore_local"])
        XCTAssertEqual(fixture.journal?.operationID, "operation")
    }

}
