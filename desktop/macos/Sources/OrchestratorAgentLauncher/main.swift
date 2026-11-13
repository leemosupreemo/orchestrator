import Foundation
import Darwin

let ownPath = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.resolvingSymlinksInPath()
let bundle = ownPath.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
let resources = bundle.appendingPathComponent("Contents/Resources")
let python = resources.appendingPathComponent("runtime/bin/python3")
guard FileManager.default.isExecutableFile(atPath: python.path) else {
    fputs("The private Orchestrator runtime is missing. Reinstall the application.\n", stderr)
    exit(78)
}
let info = (try? Data(contentsOf: bundle.appendingPathComponent("Contents/Info.plist"))).flatMap { try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: Any] } ?? [:]
let integration = info["OrchestratorIntegrationFixture"] as? Bool == true
if integration && info["CFBundleIdentifier"] as? String != "com.orchestrator.desktop.integration" { exit(78) }
var extraArguments = Array(CommandLine.arguments.dropFirst())
if integration {
    setenv("ORCHESTRATOR_USER_STATE_DIR", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".orchestrator-desktop-test").path, 1)
    if !extraArguments.contains("--control-dir") {
        extraArguments += ["--control-dir", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orchestrator/test").path]
    }
}
let arguments = [python.path, "-P", "-m", "orchestrator.desktop_agent"] + extraArguments
let privateBin = resources.appendingPathComponent("bin").path
let home = FileManager.default.homeDirectoryForCurrentUser.path
setenv("PATH", "\(privateBin):\(python.deletingLastPathComponent().path):/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin", 1)
setenv("ORCHESTRATOR_PACKAGED_APP", bundle.path, 1)
setenv("PYTHONDONTWRITEBYTECODE", "1", 1)
setenv("PYTHONNOUSERSITE", "1", 1)
unsetenv("PYTHONPATH"); unsetenv("PYTHONHOME")
let certificates = resources.appendingPathComponent("certificates/cacert.pem").path
setenv("SSL_CERT_FILE", certificates, 1); setenv("REQUESTS_CA_BUNDLE", certificates, 1)
let pointers = arguments.map { strdup($0) } + [nil]
defer { for pointer in pointers { free(pointer) } }
pointers.withUnsafeBufferPointer { _ = execv(python.path, $0.baseAddress!) }
fputs("Could not execute the bundled runtime.\n", stderr)
exit(71)
