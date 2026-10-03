import SwiftUI
import DesktopCore

struct WelcomeView: View {
    let controller: MenuController
    @ObservedObject var state: DesktopState
    @ObservedObject var services: ServiceController
    @State private var idea = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            // What they want to build comes first; connecting and settings follow.
            Text("What do you want to build?").font(.title2)
            TextField("e.g. A turn-based word game to play with friends", text: $idea)
                .textFieldStyle(.roundedBorder)
                .onSubmit { if !idea.trimmingCharacters(in: .whitespaces).isEmpty { controller.startIdea(idea) } }
                .accessibilityLabel("What do you want to build?")
            HStack {
                Button("Start") { controller.startIdea(idea) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(idea.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("I already have code…") { controller.chooseFolder() }
            }
            Text("It becomes the start of your product's plan. You can change everything later.").font(.caption).foregroundStyle(.secondary)
            Divider()
            Text("Use it from anywhere").font(.headline)
            Text("Connect this Mac to your account to open it from your phone or another computer. Your code stays here.").font(.callout)
            if let pairing = state.status?["pairing"] {
                if pairing["state"]?.string == "connected" { Text("Connected as \(pairing["owner_email"]?.string ?? "your account")") }
                else if let code = pairing["code"]?.string {
                    Text("Pairing code: \(code)").textSelection(.enabled)
                    Button("Cancel pairing") { state.perform("pair_cancel") }
                } else { Button("Connect this Mac") { controller.pair() } }
            }
            Text(state.statusText).font(.callout).foregroundStyle(.secondary)
            Toggle("Start at login", isOn: Binding(get: { services.desired }, set: { controller.setStartAtLogin($0) }))
            if services.registration == .requiresApproval { Button("Open Login Items Settings…") { services.openApprovalSettings() } }
            Button("Open Orchestrator") { controller.openBrowser("#/") }
            if !state.error.isEmpty { Text(state.error).foregroundStyle(.red).textSelection(.enabled) }
            Spacer(minLength: 0)
        }.padding(28).frame(minWidth: 460, minHeight: 440)
    }
}
