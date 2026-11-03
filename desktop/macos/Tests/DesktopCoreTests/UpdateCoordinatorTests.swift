import XCTest
@testable import DesktopCore

@MainActor final class UpdateCoordinatorTests: XCTestCase {
    @MainActor final class Fixture {
        var busy = false, unavailable = false, fail = false
        var journal: UpdateJournal?
        var events: [String] = []
        func coordinator() -> UpdateCoordinator {
            UpdateCoordinator(adapter: UpdateAdapter(prepare: {
                if self.unavailable { throw AgentError(code: "unavailable", message: "Unknown agent activity") }
                return !self.busy
            }, cancel: { self.events.append("cancel") }, stop: {
                self.events.append("stop")
                if self.fail { throw AgentError(code: "stop_failed", message: "Cannot stop") }
            }, restore: { enabled in self.events.append(enabled ? "restore_login" : "restore_local") }, desired: { true }, load: { self.journal }, save: { self.journal = $0 }))
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
}
