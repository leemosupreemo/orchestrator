import XCTest
@testable import DesktopCore

final class AgentClientTests: XCTestCase {
    func testTypedRefusal() async throws {
        let client = AgentClient(transport: { raw in
            let request = try JSONDecoder().decode(AgentRequest.self, from: raw)
            return Data("{\"version\":1,\"request_id\":\"\(request.requestID)\",\"ok\":false,\"error\":{\"code\":\"busy\",\"message\":\"Work is still running.\"}}".utf8)
        })
        do { _ = try await client.request(command: "stop_if_idle"); XCTFail("must refuse") }
        catch let error as AgentError { XCTAssertEqual(error.code, "busy") }
    }

    func testStaleReplyIsRefused() async {
        let client = AgentClient(transport: { _ in Data("{\"version\":1,\"request_id\":\"stale\",\"ok\":true,\"result\":{}}".utf8) })
        do { _ = try await client.request(command: "status"); XCTFail("stale reply") }
        catch let error as AgentError { XCTAssertEqual(error.code, "invalid_reply") }
        catch { XCTFail("unexpected error: \(error)") }
    }

    func testMissingSocketIsActionable() async {
        let client = AgentClient(socketPath: "/tmp/orchestrator-no-such-socket-\(UUID().uuidString)")
        do { _ = try await client.request(command: "status"); XCTFail("missing socket") }
        catch let error as AgentError { XCTAssertEqual(error.code, "unavailable"); XCTAssertTrue(error.message.contains("Start")) }
        catch { XCTFail("unexpected error") }
    }
}
