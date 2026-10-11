import AppKit

@main enum AppMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = MenuController()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
