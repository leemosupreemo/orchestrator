import XCTest
@testable import DesktopCore

final class DesktopRoutesTests: XCTestCase {
    func testIdeaIsCarriedIntoTheNewProjectForm() {
        XCTAssertEqual(DesktopRoutes.newProject(pitch: "  A word game  "), "#/new-project?pitch=A%20word%20game")
        XCTAssertEqual(DesktopRoutes.newProject(pitch: "Tips + tricks & more #1"), "#/new-project?pitch=Tips%20%2B%20tricks%20%26%20more%20%231")
        XCTAssertEqual(DesktopRoutes.newProject(pitch: "   "), "#/new-project")
        XCTAssertEqual(DesktopRoutes.newProject(pitch: String(repeating: "a", count: 500)).count, "#/new-project?pitch=".count + 300)
    }
    func testFolderRouteEncodesPaths() {
        XCTAssertEqual(DesktopRoutes.setup(folder: "/Users/me/My App"), "#/setup?root=%2FUsers%2Fme%2FMy%20App")
    }
}
