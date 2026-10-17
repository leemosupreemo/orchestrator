import AppKit
import SwiftUI
import DesktopCore

struct DiagnosticsView: View {
    @ObservedObject var state: DesktopState
    @State private var report = "Loading…"
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Diagnostics").font(.title2)
            Text("Only connection states and versions are included. Credentials, project files and run output are excluded.").font(.caption)
            ScrollView { Text(report).font(.system(.body, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
            HStack {
                Button("Copy") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(report, forType: .string) }
                Button("Export…") {
                    let panel = NSSavePanel(); panel.nameFieldStringValue = "orchestrator-diagnostics.json"
                    if panel.runModal() == .OK, let url = panel.url { do { try report.write(to: url, atomically: true, encoding: .utf8) } catch { state.error = "Could not export diagnostics." } }
                }
            }
        }.padding(28).frame(minWidth: 440, minHeight: 340).task {
            do {
                let value = try await state.command("diagnostics")
                let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
                report = String(decoding: try encoder.encode(value), as: UTF8.self)
            } catch { report = "Agent unavailable. Launch Orchestrator again before exporting diagnostics." }
        }
    }
}
