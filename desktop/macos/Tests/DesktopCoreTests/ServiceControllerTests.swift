import XCTest
@testable import DesktopCore

@MainActor final class ServiceControllerTests: XCTestCase {
    @MainActor final class Fixture {
        var registration: RegistrationState = .notRegistered
        var busy = false
        var events: [String] = []
        var store: [String: Bool] = [:]
        func controller(bundle: URL = URL(fileURLWithPath: "/Applications/Orchestrator.app")) -> ServiceController {
            let adapter = ServiceAdapter(state: { self.registration },
                register: { self.events.append("register"); self.registration = .enabled },
                unregister: { self.events.append("unregister"); self.registration = .notRegistered },
                prepare: { self.events.append("prepare"); return !self.busy },
                cancel: { self.events.append("cancel") },
                stop: { self.events.append("stop") },
                waitStopped: { self.events.append("wait") },
                start: { self.events.append("start") },
                load: { self.store[$0] ?? false }, save: { self.store[$0] = $1 })
            return ServiceController(bundle: bundle, adapter: adapter)
        }
    }
    func testRegistrationIsIndependentOfHealth() {
        let fixture = Fixture(), controller = Fixture().controller()
        XCTAssertEqual(controller.registrationState(), .notRegistered)
        for state in [RegistrationState.enabled, .requiresApproval, .notFound] {
            fixture.registration = state
            XCTAssertEqual(fixture.controller().registrationState(), state)
        }
    }
    func testBusyModeChangeWaitsWithoutUnregistering() async throws {
        let fixture = Fixture(); fixture.registration = .enabled; fixture.store["startAtLogin"] = true; fixture.busy = true
        let controller = fixture.controller()
        try await controller.setStartAtLogin(false)
        XCTAssertTrue(controller.pending)
        XCTAssertEqual(fixture.events, ["prepare"])
        fixture.busy = false
        try await controller.reconcile()
        XCTAssertFalse(controller.pending)
        XCTAssertEqual(fixture.events, ["prepare", "prepare", "unregister", "stop", "wait", "start"])
    }
    func testIdleModeStartsExactlyOneReplacement() async throws {
        let fixture = Fixture(), controller = fixture.controller()
        try await controller.setStartAtLogin(true)
        XCTAssertEqual(fixture.events, ["prepare", "unregister", "stop", "wait", "register"])
        XCTAssertEqual(controller.registrationState(), .enabled)
    }
    func testDMGCannotRegister() async {
        let fixture = Fixture(), controller = fixture.controller(bundle: URL(fileURLWithPath: "/Volumes/Orchestrator/Orchestrator.app"))
        do { try await controller.setStartAtLogin(true); XCTFail("DMG registration") }
        catch { XCTAssertTrue(error.localizedDescription.contains("Applications")) }
        XCTAssertTrue(fixture.events.isEmpty)
    }
    func testStopUnregistersKeepAliveAndStaysSuspended() async throws {
        let fixture = Fixture(); fixture.registration = .enabled; fixture.store["startAtLogin"] = true
        let controller = fixture.controller()
        try await controller.stopIfIdle()
        XCTAssertTrue(controller.suspended)
        XCTAssertTrue(controller.desired)
        try await controller.reconcile()
        XCTAssertFalse(fixture.events.contains("start"))
        XCTAssertEqual(fixture.registration, .notRegistered)
    }
    func testFailureRestoresPreviousMode() async {
        let fixture = Fixture()
        let adapter = ServiceAdapter(state: { .notRegistered }, register: { throw AgentError(code: "registration_failed", message: "Approval failed") }, unregister: {}, prepare: { true }, cancel: { fixture.events.append("cancel") }, stop: {}, waitStopped: {}, start: { fixture.events.append("restore") }, load: { _ in false }, save: { _, _ in })
        let controller = ServiceController(bundle: URL(fileURLWithPath: "/Applications/Orchestrator.app"), adapter: adapter)
        do { try await controller.setStartAtLogin(true); XCTFail("registration failure") } catch {}
        XCTAssertEqual(fixture.events, ["cancel", "restore"])
    }
    func testRemovalKeepsUserDataAndDoesNotRestart() async throws {
        let fixture = Fixture(); fixture.registration = .enabled; fixture.store["startAtLogin"] = true
        let controller = fixture.controller()
        try await controller.removeBackgroundComponents()
        XCTAssertFalse(controller.desired)
        XCTAssertTrue(controller.suspended)
        XCTAssertEqual(fixture.events, ["prepare", "unregister", "stop", "wait"])
    }
}
