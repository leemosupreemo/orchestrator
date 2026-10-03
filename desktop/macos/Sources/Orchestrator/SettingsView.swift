import SwiftUI
import DesktopCore

struct SettingsView: View {
    let controller: MenuController
    @ObservedObject var state: DesktopState
    @ObservedObject var services: ServiceController
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Orchestrator Settings").font(.title2)
            Text(state.statusText)
            Text(state.activityText).foregroundStyle(.secondary)
            Text("Account: \(state.status?["pairing"]?["owner_email"]?.string ?? "Not connected")")
            Button("Connect this Mac…") { controller.pair(); controller.showWelcome() }
            Toggle("Remote access", isOn: Binding(get: { state.status?["remote_enabled"]?.bool ?? false }, set: { state.perform("remote_access", params: ["enabled": .bool($0)]) })).disabled(state.status == nil)
            Toggle("Start at login", isOn: Binding(get: { services.desired }, set: { controller.setStartAtLogin($0) }))
            Text("Background permission: \(services.registration.rawValue)").font(.caption)
            if services.pending { Text("Waiting for current work to finish before changing background mode.").font(.caption) }
            if services.registration == .requiresApproval { Button("Open Login Items Settings…") { services.openApprovalSettings() } }
            Button("Stop Orchestrator on this Mac") { controller.stop() }.disabled(!state.canStop)
            Text(state.canStop ? "Stopping leaves your settings and projects intact." : "Stop is unavailable while work is running or activity is unknown.").font(.caption).foregroundStyle(.secondary)
            if let legacy = state.legacyText {
                Text(legacy).font(.callout).fixedSize(horizontal: false, vertical: true)
                HStack {
                    if state.canMigrateLegacy { Button("Move to the app…") { controller.migrateLegacy() } }
                    Button("Check Again") { state.perform("legacy_status") }
                }
            }
            Text("Version: \(state.status?["runner"]?["version"]?.string ?? "Unknown")")
            Button("Remove background components…") { controller.removeBackgroundComponents() }.disabled(!state.canStop)
            if !state.operation.isEmpty { Text(state.operation).font(.callout) }
            if !state.error.isEmpty { Text(state.error).foregroundStyle(.red).textSelection(.enabled) }
            Spacer(minLength: 0)
        }.padding(28).frame(minWidth: 440, minHeight: 380)
    }
}
