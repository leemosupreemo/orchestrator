import XCTest
@testable import DesktopCore

@MainActor final class RemoteUpdateSessionTests: XCTestCase {
    func testCheckingCancellationSuppressesLateDownloadAndDisarmsReadyInstaller() {
        let session = RemoteUpdateSession(id: "first")
        session.cancel()
        XCTAssertFalse(session.active)
        var stopped = false, skipped = false, installed = false
        session.downloadStarted(cancel: { stopped = true })
        session.ready(install: { installed = true }, cancel: { skipped = true })
        session.install()
        XCTAssertTrue(stopped)
        XCTAssertTrue(skipped)
        XCTAssertFalse(installed)
    }
    func testWaitingCancellationSkipsInstallerExactlyOnce() {
        let session = RemoteUpdateSession(id: "first")
        var skipped = 0, installed = false
        session.ready(install: { installed = true }, cancel: { skipped += 1 })
        session.cancel(); session.cancel(); session.install()
        XCTAssertEqual(skipped, 1)
        XCTAssertFalse(installed)
    }
    func testInstallingSessionCannotBeCancelledAndOldSessionCannotInstallNewOne() {
        let old = RemoteUpdateSession(id: "old"), new = RemoteUpdateSession(id: "new")
        var installed = 0
        old.cancel()
        new.ready(install: { installed += 1 }, cancel: {})
        old.install()
        XCTAssertEqual(installed, 0)
        new.install()
        XCTAssertFalse(new.cancel())
        XCTAssertEqual(installed, 1)
    }
    func testDownloadingCancellationStopsActualDownload() {
        let session = RemoteUpdateSession(id: "first")
        var stopped = 0
        session.downloadStarted(cancel: { stopped += 1 })
        XCTAssertTrue(session.cancel())
        XCTAssertEqual(stopped, 1)
    }
    func testCancellationDuringExtractionWaitsForReadySkipAndDoesNotReuseDownloadHook() {
        let session = RemoteUpdateSession(id: "first")
        var stopped = 0, skipped = 0
        session.downloadStarted(cancel: { stopped += 1 })
        session.extractionStarted()
        session.cancel()
        XCTAssertFalse(session.cancellationComplete)
        XCTAssertEqual(stopped, 0)
        session.ready(install: {}, cancel: { skipped += 1 })
        XCTAssertTrue(session.cancellationComplete)
        XCTAssertEqual(skipped, 1)
    }

    func testReadyReleasesDownloadHookAndSuccessfulInstallRepliesOnlyOnce() {
        let session = RemoteUpdateSession(id: "first")
        var stopped = 0, installed = 0, skipped = 0
        session.downloadStarted(cancel: { stopped += 1 })
        session.ready(install: { installed += 1 }, cancel: { skipped += 1 })
        session.install(); session.install()
        XCTAssertFalse(session.cancel())
        XCTAssertEqual(installed, 1)
        XCTAssertEqual(stopped, 0)
        XCTAssertEqual(skipped, 0)
    }

    func testCycleOutcomeDistinguishesNoReleaseFromFailureAndNeverOverwritesCancellation() {
        let session = RemoteUpdateSession(id: "first")
        XCTAssertEqual(session.terminalState(errorCode: 1001), "current")
        XCTAssertEqual(session.terminalState(errorCode: 4000), "failed")
        session.cancel()
        XCTAssertNil(session.terminalState(errorCode: 4000))
    }

}
