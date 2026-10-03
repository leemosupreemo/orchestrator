// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OrchestratorDesktop",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "Orchestrator", targets: ["Orchestrator"]),
               .executable(name: "OrchestratorAgentLauncher", targets: ["OrchestratorAgentLauncher"])],
    dependencies: [.package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.7.3")],
    targets: [.target(name: "DesktopCore"),
              .executableTarget(name: "Orchestrator", dependencies: ["DesktopCore", .product(name: "Sparkle", package: "Sparkle")]),
              .executableTarget(name: "OrchestratorAgentLauncher"),
              .testTarget(name: "DesktopCoreTests", dependencies: ["DesktopCore"])])
