import SwiftUI
import DesktopCore

struct SettingsView: View {
    let controller: MenuController
    @ObservedObject var state: DesktopState
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Orchestrator Settings").font(.title2)
            Text(state.statusText)
            Text(state.activityText).foregroundStyle(.secondary)
            Text("Account: \(state.status?["pairing"]?["owner_email"]?.string ?? "Not connected")")
            Button("Connect this Mac…") { controller.pair(); controller.showWelcome() }
            Toggle("Remote access", isOn: Binding(get: { state.status?["remote_enabled"]?.bool ?? false }, set: { state.perform("remote_access", params: ["enabled": .bool($0)]) })).disabled(state.status == nil)
            Text("Start at login: not enabled in this development checkpoint.").font(.caption)
            Button("Stop Orchestrator on this Mac") { controller.stop() }.disabled(!state.canStop)
            Text(state.canStop ? "Stopping leaves your settings and projects intact." : "Stop is unavailable while work is running or activity is unknown.").font(.caption).foregroundStyle(.secondary)
            Text("Version: \(state.status?["runner"]?["version"]?.string ?? "Unknown")")
            if !state.operation.isEmpty { Text(state.operation).font(.callout) }
            if !state.error.isEmpty { Text(state.error).foregroundStyle(.red).textSelection(.enabled) }
            Spacer(minLength: 0)
        }.padding(28).frame(minWidth: 440, minHeight: 380)
    }
}
