import SwiftUI
import DesktopCore

struct WelcomeView: View {
    let controller: MenuController
    @ObservedObject var state: DesktopState
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Orchestrator on this Mac", systemImage: "terminal").font(.title2)
            Text("Keep your projects and work on your computer. Use the browser workspace locally or from another device.")
            Text(state.statusText).font(.callout).foregroundStyle(.secondary)
            if let pairing = state.status?["pairing"] {
                if pairing["state"]?.string == "connected" { Text("Connected as \(pairing["owner_email"]?.string ?? "your account")") }
                else if let code = pairing["code"]?.string {
                    Text("Pairing code: \(code)").textSelection(.enabled)
                    Button("Cancel pairing") { state.perform("pair_cancel") }
                } else { Button("Connect this Mac") { controller.pair() } }
            }
            HStack {
                Button("Choose project folder…") { controller.chooseFolder() }
                Button("Create a project…") { controller.openBrowser("#/new-project") }
            }
            Text("Git, GitHub, AI tools and your project's build tools may require their own installation and sign-in. Pairing and local setup work before those are ready.").font(.caption).foregroundStyle(.secondary)
            Button("Open Orchestrator") { controller.openBrowser("#/") }.keyboardShortcut(.defaultAction)
            if !state.error.isEmpty { Text(state.error).foregroundStyle(.red).textSelection(.enabled) }
            Spacer(minLength: 0)
        }.padding(28).frame(minWidth: 440, minHeight: 360)
    }
}
