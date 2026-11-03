import XCTest
@testable import DesktopCore

@MainActor final class UpdateSchedulerTests: XCTestCase {
    @MainActor final class Fixture {
        var time = Date(timeIntervalSince1970: 1_000), refuse = false
        var events: [String] = [], saved: [Bool] = []
        func scheduler(enabled: Bool = true) -> UpdateScheduler {
            UpdateScheduler(enabled: enabled, now: { self.time }, save: { self.saved.append($0) }, prepare: {
                self.events.append("prepare")
                if self.refuse { throw AgentError(code: "busy", message: "Work is running.") }
            })
        }
        func downloaded(_ scheduler: UpdateScheduler) { scheduler.updateDownloaded(install: { self.events.append("install") }) }
        func advance(_ seconds: TimeInterval) { time = time.addingTimeInterval(seconds) }
    }
    func testInstallsOnlyAfterTenIdleMinutes() async {
        let fixture = Fixture(), scheduler = fixture.scheduler()
        fixture.downloaded(scheduler)
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired - 1)
        let early = await scheduler.observe(.idle)
        XCTAssertFalse(early)
        XCTAssertEqual(fixture.events, [])
        fixture.advance(1)
        let installed = await scheduler.observe(.idle)
        XCTAssertTrue(installed)
        XCTAssertEqual(fixture.events, ["prepare", "install"])
        XCTAssertFalse(scheduler.waiting)
    }
    func testWorkOrUnknownActivityRestartsTheIdleWait() async {
        let fixture = Fixture(), scheduler = fixture.scheduler()
        fixture.downloaded(scheduler)
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired - 60)
        await scheduler.observe(.busy)
        await scheduler.observe(.idle)
        fixture.advance(120)
        await scheduler.observe(.unknown)
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired - 1)
        await scheduler.observe(.idle)
        XCTAssertEqual(fixture.events, [])
        XCTAssertTrue(scheduler.waiting)
    }
    func testRefusedGateKeepsTheUpdateAndWaitsAgain() async {
        let fixture = Fixture(), scheduler = fixture.scheduler()
        fixture.downloaded(scheduler); fixture.refuse = true
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired)
        let refused = await scheduler.observe(.idle)
        XCTAssertFalse(refused)
        XCTAssertEqual(fixture.events, ["prepare"])
        fixture.refuse = false
        fixture.advance(1)
        await scheduler.observe(.idle)
        XCTAssertEqual(fixture.events, ["prepare"])
        fixture.advance(UpdateScheduler.idleRequired)
        await scheduler.observe(.idle)
        XCTAssertEqual(fixture.events, ["prepare", "prepare", "install"])
    }
    func testRequestSkipsTheIdleWaitButNeverTheWorkGate() async {
        let fixture = Fixture(), scheduler = fixture.scheduler(enabled: false)
        fixture.downloaded(scheduler)
        scheduler.request()
        let busy = await scheduler.observe(.busy)
        XCTAssertFalse(busy)
        XCTAssertEqual(fixture.events, [])
        let idle = await scheduler.observe(.idle)
        XCTAssertTrue(idle)
        XCTAssertEqual(fixture.events, ["prepare", "install"])
        XCTAssertFalse(scheduler.requested)
    }
    func testDisabledNeverInstallsOnItsOwn() async {
        let fixture = Fixture(), scheduler = fixture.scheduler(enabled: false)
        fixture.downloaded(scheduler)
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired * 10)
        await scheduler.observe(.idle)
        XCTAssertEqual(fixture.events, [])
    }
    func testNothingDownloadedMeansNothingToInstall() async {
        let fixture = Fixture(), scheduler = fixture.scheduler()
        await scheduler.observe(.idle)
        fixture.advance(UpdateScheduler.idleRequired)
        let installed = await scheduler.observe(.idle)
        XCTAssertFalse(installed)
        XCTAssertEqual(fixture.events, [])
    }
    func testSettingPersists() {
        let fixture = Fixture(), scheduler = fixture.scheduler()
        scheduler.setEnabled(false)
        XCTAssertFalse(scheduler.enabled)
        XCTAssertEqual(fixture.saved, [false])
    }
}
