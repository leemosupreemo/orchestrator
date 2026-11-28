import XCTest
import Darwin
@testable import DesktopCore

final class InstanceLeaseTests: XCTestCase {
    func testSocketDisappearanceDoesNotMeanRuntimeLeaseReleased() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("orch-lease-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("ui-instance.lock")
        let fd = Darwin.open(file.path, O_CREAT | O_RDWR, 0o600)
        defer { Darwin.close(fd) }
        XCTAssertEqual(flock(fd, LOCK_EX | LOCK_NB), 0)
        XCTAssertFalse(AgentInstanceLease.isAvailable(stateDirectory: root.path))
        XCTAssertEqual(flock(fd, LOCK_UN), 0)
        XCTAssertTrue(AgentInstanceLease.isAvailable(stateDirectory: root.path))
    }
}
