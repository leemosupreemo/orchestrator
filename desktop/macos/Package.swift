// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OrchestratorDesktop",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "Orchestrator", targets: ["Orchestrator"]),
               .executable(name: "OrchestratorAgentLauncher", targets: ["OrchestratorAgentLauncher"])],
    targets: [.target(name: "DesktopCore"),
              .executableTarget(name: "Orchestrator", dependencies: ["DesktopCore"]),
              .executableTarget(name: "OrchestratorAgentLauncher"),
              .testTarget(name: "DesktopCoreTests", dependencies: ["DesktopCore"])])
