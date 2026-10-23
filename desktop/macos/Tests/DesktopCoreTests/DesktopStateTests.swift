import XCTest
@testable import DesktopCore

@MainActor final class DesktopStateTests: XCTestCase {
    func testLocalReadyWithRemoteOff() async throws {
        let state = DesktopState(request: { _, _ in AgentReply(result: .object([
            "agent": .string("running"), "local_interface": .string("ready"), "remote_access": .string("off"),
            "activity": .object(["runs": .number(0), "tasks": .number(0)])])) })
        await state.refresh()
        XCTAssertEqual(state.statusText, "Available locally · Remote access off")
        XCTAssertTrue(state.canStop)
    }
    func testRunningLegacyServicePausesRemoteAndCannotBeMigrated() async {
        let state = DesktopState(request: { _, _ in AgentReply(result: .object([
            "local_interface": .string("ready"), "remote_access": .string("waiting_for_legacy"), "legacy": .string("running")])) })
        await state.refresh()
        XCTAssertEqual(state.statusText, "Available locally · Remote access paused")
        XCTAssertTrue(state.legacyText?.contains("orchestrator service uninstall") == true)
        XCTAssertFalse(state.canMigrateLegacy)
    }
    func testStoppedLegacyServiceOffersMigrationAndNoneIsQuiet() async {
        var legacy = "stopped"
        let state = DesktopState(request: { _, _ in AgentReply(result: .object(["legacy": .string(legacy)])) })
        await state.refresh()
        XCTAssertTrue(state.canMigrateLegacy)
        legacy = "none"
        await state.refresh()
        XCTAssertNil(state.legacyText)
        XCTAssertFalse(state.canMigrateLegacy)
    }
    func testActivityForUpdatesIsUnknownUnlessTheAgentAnswers() async {
        var reply: JSONValue = .object(["agent": .string("running"), "activity": .object(["runs": .number(0), "tasks": .number(0)])])
        let state = DesktopState(request: { _, _ in AgentReply(result: reply) })
        XCTAssertEqual(state.activity, .unknown)
        await state.refresh()
        XCTAssertEqual(state.activity, .idle)
        reply = .object(["agent": .string("running"), "activity": .object(["runs": .number(1), "tasks": .number(0)])])
        await state.refresh()
        XCTAssertEqual(state.activity, .busy)
        reply = .object(["agent": .string("running")])
        await state.refresh()
        XCTAssertEqual(state.activity, .unknown)
    }
    func testUnknownActivityDisablesStop() async {
        let state = DesktopState(request: { _, _ in AgentReply(result: .object(["agent": .string("running")])) })
        await state.refresh()
        XCTAssertFalse(state.canStop)
    }
    func testDisconnectRemovesPreviouslyKnownIdleState() async {
        let state = DesktopState(request: { _, _ in throw AgentError(code: "unavailable", message: "Start Orchestrator again.") })
        await state.refresh()
        XCTAssertFalse(state.canStop)
        XCTAssertTrue(state.statusText.contains("unavailable"))
        XCTAssertTrue(state.error.contains("Start"))
    }
    func testOlderRefreshCannotOverwriteNewerResponse() async {
        var count = 0
        let state = DesktopState(request: { _, _ in
            count += 1
            let current = count
            if current == 1 { try await Task.sleep(nanoseconds: 50_000_000) }
            return AgentReply(result: .object(["remote_access": .string(current == 1 ? "connected" : "off")]))
        })
        async let first: Void = state.refresh()
        try? await Task.sleep(nanoseconds: 5_000_000)
        await state.refresh()
        await first
        XCTAssertEqual(state.status?["remote_access"]?.string, "off")
    }
}
