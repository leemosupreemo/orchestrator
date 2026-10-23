import Foundation
import Darwin

public enum AgentInstanceLease {
    public static func isAvailable(stateDirectory: String) -> Bool {
        let path = URL(fileURLWithPath: stateDirectory).appendingPathComponent("ui-instance.lock").path
        let fd = Darwin.open(path, O_RDWR | O_NOFOLLOW)
        guard fd >= 0 else { return errno == ENOENT }
        defer { Darwin.close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_nlink == 1, info.st_mode & S_IFMT == S_IFREG else { return false }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { return false }
        _ = flock(fd, LOCK_UN)
        return true
    }
}
