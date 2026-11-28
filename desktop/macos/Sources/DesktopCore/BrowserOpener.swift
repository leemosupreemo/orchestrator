import Foundation

public struct BrowserOpener {
    private let open: (URL) -> Void
    public init(open: @escaping (URL) -> Void) { self.open = open }
    private func sameOrigin(_ url: URL, _ origin: URL) -> Bool {
        url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port && url.user == nil && url.password == nil
    }
    public func openGrant(_ result: BrowserGrantResult, trustedOrigin: URL) throws {
        guard trustedOrigin.scheme == "http", trustedOrigin.host == "127.0.0.1", trustedOrigin.port != nil,
              let url = URL(string: result.url), sameOrigin(url, trustedOrigin), url.path == "/desktop/open", url.fragment == nil,
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              items.count == 1, items[0].name == "grant", !(items[0].value ?? "").isEmpty else {
            throw AgentError(code: "unsafe_url", message: "The agent returned an unsafe browser address. Nothing was opened.")
        }
        open(url)
    }
    public func openPairing(_ address: String, trustedOrigin: URL) throws {
        guard trustedOrigin.scheme == "https", let url = URL(string: address), sameOrigin(url, trustedOrigin), url.path == "/",
              url.query == nil, let fragment = url.fragment, fragment.hasPrefix("/connect?code="),
              fragment.dropFirst("/connect?code=".count).allSatisfy({ $0.isASCII && ($0.isNumber || $0.isLetter) }),
              fragment.dropFirst("/connect?code=".count).count == 8 else {
            throw AgentError(code: "unsafe_url", message: "The pairing address is not the configured account service.")
        }
        open(url)
    }
}
