import XCTest
@testable import DesktopCore

final class BrowserOpenerTests: XCTestCase {
    func testGrantOnlyOpensExactLocalOrigin() throws {
        var opened: URL?
        let opener = BrowserOpener(open: { opened = $0 })
        let origin = URL(string: "http://127.0.0.1:9000")!
        try opener.openGrant(BrowserGrantResult(url: "http://127.0.0.1:9000/desktop/open?grant=one"), trustedOrigin: origin)
        XCTAssertEqual(opened?.host, "127.0.0.1")
        for url in ["https://evil.example/desktop/open?grant=one", "http://127.0.0.1:9001/desktop/open?grant=one", "http://user@127.0.0.1:9000/desktop/open?grant=one", "http://127.0.0.1:9000/api/run?grant=one"] {
            XCTAssertThrowsError(try opener.openGrant(BrowserGrantResult(url: url), trustedOrigin: origin))
        }
    }
    func testPairingRequiresConfiguredHostedOrigin() throws {
        let opener = BrowserOpener(open: { _ in })
        let origin = URL(string: "https://account.example")!
        try opener.openPairing("https://account.example/#/connect?code=12345678", trustedOrigin: origin)
        try opener.openPairing("https://account.example/#/connect?code=ABCD2345", trustedOrigin: origin)
        XCTAssertThrowsError(try opener.openPairing("https://account.example.evil/#/connect?code=12345678", trustedOrigin: origin))
        XCTAssertThrowsError(try opener.openPairing("https://account.example/#/settings", trustedOrigin: origin))
    }
}
