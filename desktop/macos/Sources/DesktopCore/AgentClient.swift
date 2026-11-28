import Foundation
import Darwin

public struct AgentClient {
    public static var defaultSocketPath: String {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orchestrator/control/agent.sock").path
    }
    private let transport: (Data) throws -> Data
    public init(socketPath: String = AgentClient.defaultSocketPath) {
        transport = { try Self.exchange($0, path: socketPath) }
    }
    public init(transport: @escaping (Data) throws -> Data) { self.transport = transport }
    public func request(command: String, params: [String: JSONValue] = [:]) async throws -> AgentReply {
        let request = AgentRequest(version: 1, requestID: UUID().uuidString, command: command, params: params)
        let data = try JSONEncoder().encode(request)
        let transport = self.transport
        return try await Task.detached {
            do {
                let raw = try transport(data)
                guard raw.count <= 65536 else { throw AgentError(code: "invalid_reply", message: "The agent reply was too large.") }
                let reply = try JSONDecoder().decode(AgentReply.self, from: raw)
                guard reply.version == 1, reply.requestID == request.requestID, reply.ok ? reply.result != nil : reply.error != nil else {
                    throw AgentError(code: "invalid_reply", message: "The app and agent replies do not match. Restart Orchestrator.")
                }
                if !reply.ok { throw reply.error! }
                return reply
            } catch let error as AgentError { throw error }
            catch { throw AgentError(code: "invalid_reply", message: "The agent sent an unreadable reply. Restart Orchestrator.") }
        }.value
    }
    private static func exchange(_ data: Data, path: String) throws -> Data {
        let unavailable = AgentError(code: "unavailable", message: "Agent unavailable. Start Orchestrator again, or open Diagnostics.")
        guard data.count + 1 <= 65536, path.utf8.count < 104 else { throw AgentError(code: "invalid_request", message: "Desktop control path or request is too long.") }
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw unavailable }
        defer { Darwin.close(fd) }
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var noSignal: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: Array(path.utf8) + [0])
        }
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard connected == 0 else { throw unavailable }
        var uid: uid_t = 0, gid: gid_t = 0
        guard getpeereid(fd, &uid, &gid) == 0, uid == getuid() else {
            throw AgentError(code: "unauthorized", message: "The desktop agent belongs to another Mac user.")
        }
        let frame = data + Data([10])
        try frame.withUnsafeBytes { bytes in
            var sent = 0
            while sent < frame.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: sent), frame.count - sent)
                guard count > 0 else { throw unavailable }
                sent += count
            }
        }
        var reply = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while reply.count <= 65536 {
            let count = Darwin.read(fd, &chunk, chunk.count)
            guard count > 0 else { throw unavailable }
            if let newline = chunk.prefix(count).firstIndex(of: 10) {
                reply.append(contentsOf: chunk.prefix(newline))
                return reply
            }
            reply.append(contentsOf: chunk.prefix(count))
        }
        throw AgentError(code: "invalid_reply", message: "The agent reply was too large.")
    }
}
