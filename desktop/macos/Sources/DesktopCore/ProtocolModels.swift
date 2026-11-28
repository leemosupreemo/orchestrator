import Foundation

public enum JSONValue: Codable, Equatable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let v = try? value.decode(Bool.self) { self = .bool(v) }
        else if let v = try? value.decode(Double.self) { self = .number(v) }
        else if let v = try? value.decode(String.self) { self = .string(v) }
        else if let v = try? value.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .object(let v): try value.encode(v)
        case .array(let v): try value.encode(v)
        case .string(let v): try value.encode(v)
        case .number(let v): try value.encode(v)
        case .bool(let v): try value.encode(v)
        case .null: try value.encodeNil()
        }
    }
    public subscript(key: String) -> JSONValue? { if case .object(let v) = self { return v[key] }; return nil }
    public var string: String? { if case .string(let v) = self { return v }; return nil }
    public var bool: Bool? { if case .bool(let v) = self { return v }; return nil }
    public var int: Int? { if case .number(let v) = self, v.isFinite, v >= 0, v < Double(Int.max), v.rounded() == v { return Int(v) }; return nil }
}

public struct AgentRequest: Codable {
    public let version: Int
    public let requestID, command: String
    public let params: [String: JSONValue]
    enum CodingKeys: String, CodingKey { case version, requestID = "request_id", command, params }
}
public struct AgentError: Error, Codable, LocalizedError {
    public let code, message: String
    public init(code: String, message: String) { self.code = code; self.message = message }
    public var errorDescription: String? { message }
}
public struct AgentReply: Codable {
    public let version: Int
    public let requestID: String
    public let ok: Bool
    public let result: JSONValue?
    public let error: AgentError?
    enum CodingKeys: String, CodingKey { case version, requestID = "request_id", ok, result, error }
    public init(result: JSONValue) { version = 1; requestID = "fixture"; ok = true; self.result = result; error = nil }
}
public struct BrowserGrantResult: Codable {
    public let url: String
    public init(url: String) { self.url = url }
}
