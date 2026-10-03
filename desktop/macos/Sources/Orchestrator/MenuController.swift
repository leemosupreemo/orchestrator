import AppKit
import SwiftUI
import DesktopCore
import Darwin

@MainActor final class MenuController: NSObject, NSApplicationDelegate, NSMenuDelegate, ObservableObject {
    let state: DesktopState
    let client: AgentClient
    lazy var services = ServiceController(bundle: Bundle.main.bundleURL, adapter: .live(client: client, start: { [weak self] in
        guard let self else { return }
        try self.launchAgent()
    }))
    lazy var updateCoordinator = UpdateCoordinator(adapter: .live(client: client, services: services))
    lazy var updater: SparkleAdapter = {
        let value = SparkleAdapter(coordinator: updateCoordinator)
        value.displayError = { [weak self] message in self?.state.operation = message; self?.showSettings() }
        return value
    }()
    let opener = BrowserOpener(open: { NSWorkspace.shared.open($0) })
    let hostedOrigin = URL(string: "https://swift-orch-web-20260923.web.app")!
    private var item: NSStatusItem!
    private var poweringOff = false
    private var windows: [String: NSWindow] = [:]
    private var timer: Timer?
    private var lease: Int32 = -1
    private var openedPairing: String?
    private var refreshPending = false
    private var recordedRegistration = ""
    /// Set by `orchestrator enroll` over SSH: register login items and start the agent without showing a window.
    private let registerBackground = CommandLine.arguments.contains("--register-background")
    private var registerName: Notification.Name { Notification.Name(activationName.rawValue + ".register") }
    private var activationName: Notification.Name {
        Notification.Name((Bundle.main.bundleIdentifier ?? "com.orchestrator.desktop") + ".show")
    }
    override init() {
        let client = AgentClient(socketPath: DesktopPaths.controlDirectory + "/agent.sock")
        self.client = client
        state = DesktopState(request: { command, params in try await client.request(command: command, params: params) })
        super.init()
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let directory = URL(fileURLWithPath: DesktopPaths.controlDirectory)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            lease = Darwin.open(directory.appendingPathComponent("menu.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            var info = stat()
            guard lease >= 0, fstat(lease, &info) == 0, info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFREG else {
                throw AgentError(code: "unsafe_lock", message: "The menu lock is unsafe. Open Diagnostics before retrying.")
            }
            guard flock(lease, LOCK_EX | LOCK_NB) == 0 else {
                DistributedNotificationCenter.default().postNotificationName(registerBackground ? registerName : activationName, object: Bundle.main.bundlePath, userInfo: nil, deliverImmediately: true)
                NSApplication.shared.terminate(nil)
                return
            }
            _ = fchmod(lease, 0o600)
        } catch { showStartupError(error.localizedDescription); return }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(reopen), name: activationName, object: Bundle.main.bundlePath)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(registerFromCommand), name: registerName, object: Bundle.main.bundlePath)
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.poweringOff = true }
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "terminal", accessibilityDescription: "Orchestrator")
        item.button?.image?.isTemplate = true
        item.button?.setAccessibilityLabel("Orchestrator status and controls")
        rebuildMenu()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in Task { @MainActor in await self?.refresh() } }
        Task {
            do {
                try await updateCoordinator.recoverAfterRelaunch()
                if registerBackground { await registerInBackground() }
                else { try await lifecycle.ensureRunning() }
            } catch { state.error = error.localizedDescription }
            await refresh()
            if state.status?["setup"]?.string != "ready" && !registerBackground { showWelcome() }
            if CommandLine.arguments.contains("--smoke-menu") {
                print("Menu smoke: \(state.statusText)")
                NSApplication.shared.terminate(nil)
            }
        }
    }
    private var lifecycle: LifecycleCoordinator {
        LifecycleCoordinator(client: client, services: services, start: { [weak self] in try self?.launchAgent() })
    }
    @objc func registerFromCommand() { Task { await registerInBackground() } }
    func registerInBackground() async {
        do { _ = try await services.registerInBackground(ensureRunning: { try await lifecycle.ensureRunning() }) }
        catch { state.error = error.localizedDescription }
        recordRegistration(force: true)
    }
    private func recordRegistration(force: Bool = false) {
        let current = services.registrationState().rawValue + (services.pending ? "+pending" : "")
        guard force || current != recordedRegistration else { return }
        recordedRegistration = current
        ServiceController.recordRegistration(services.registrationState(), pending: services.pending, in: DesktopPaths.controlDirectory)
    }
    func launchAgent() throws {
        let launcher = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/OrchestratorAgentLauncher")
        guard FileManager.default.isExecutableFile(atPath: launcher.path) else {
            throw AgentError(code: "missing_runtime", message: "The bundled agent is missing. Reinstall Orchestrator, or use the development fixture agent.")
        }
        do {
            let process = Process()
            process.executableURL = launcher
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
        } catch { throw AgentError(code: "start_failed", message: "Could not start the bundled agent. Open Diagnostics.") }
    }
    func refresh() async {
        guard !refreshPending else { return }
        refreshPending = true
        defer { refreshPending = false }
        await state.refresh()
        recordRegistration()
        if services.pending, state.canStop {
            do { try await services.reconcile() }
            catch { state.error = error.localizedDescription }
        }
        if let pairing = state.status?["pairing"], pairing["state"]?.string == "waiting", let url = pairing["url"]?.string, url != openedPairing {
            do { try opener.openPairing(url, trustedOrigin: hostedOrigin); openedPairing = url }
            catch { state.error = error.localizedDescription }
        }
        rebuildMenu()
    }
    func menuWillOpen(_ menu: NSMenu) { Task { await refresh() } }
    private func rebuildMenu() {
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        add("Orchestrator", to: menu)
        add(state.statusText, to: menu)
        add("Project: \(state.status?["project"]?["name"]?.string ?? "Not selected")", to: menu)
        add(state.activityText, to: menu)
        menu.addItem(.separator())
        add("Open Orchestrator…", action: #selector(openWorkspace), key: "o", to: menu)
        add("Projects…", action: #selector(openProjects), to: menu)
        let remote = add("Remote access", action: #selector(toggleRemote), to: menu)
        remote.state = state.status?["remote_enabled"]?.bool == true ? .on : .off
        remote.isEnabled = state.status != nil
        menu.addItem(.separator())
        add("Settings…", action: #selector(showSettings), key: ",", to: menu)
        add("Check for Updates…", action: #selector(checkUpdates), to: menu)
        add("Diagnostics…", action: #selector(showDiagnostics), to: menu)
        add("Quit Orchestrator Menu", action: #selector(quitMenu), key: "q", to: menu)
        item?.menu = menu
    }
    @discardableResult private func add(_ title: String, action: Selector? = nil, key: String = "", to menu: NSMenu) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.target = self
        entry.isEnabled = action != nil
        menu.addItem(entry)
        return entry
    }
    func openBrowser(_ route: String) {
        Task {
            do {
                await state.refresh()
                guard let address = state.status?["local_origin"]?.string, let origin = URL(string: address) else { throw AgentError(code: "unavailable", message: "Start Orchestrator before opening the workspace.") }
                let result = try await state.command("browser_grant", params: ["route": .string(route)])
                guard let url = result["url"]?.string else { throw AgentError(code: "invalid_reply", message: "No browser address was returned.") }
                try opener.openGrant(BrowserGrantResult(url: url), trustedOrigin: origin)
            } catch { state.error = error.localizedDescription; showSettings() }
        }
    }
    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.allowsMultipleSelection = false; panel.prompt = "Choose project"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        openBrowser("#/setup?root=" + (url.path.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""))
    }
    func pair() { openedPairing = nil; state.perform("pair_start", params: ["name": .string(Host.current().localizedName ?? "Mac")]) }
    func stop() {
        Task {
            do {
                try await services.stopIfIdle()
                await state.refresh()
            } catch { state.error = error.localizedDescription }
        }
    }
    func setStartAtLogin(_ enabled: Bool) {
        Task {
            do { try await services.setStartAtLogin(enabled) }
            catch { state.error = error.localizedDescription }
        }
    }
    func migrateLegacy() {
        let alert = NSAlert()
        alert.messageText = "Move to the Orchestrator app?"
        alert.informativeText = "This removes the older background service set up with `orchestrator service install`. It isn't running, so no work is stopped. A copy of its definition is kept, and your projects, settings and account connection stay as they are."
        alert.addButton(withTitle: "Move to the app"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            do {
                let result = try await state.command("legacy_migrate", params: ["consent": .bool(true)])
                if result["migrated"]?.bool == true {
                    state.operation = "The older service was removed. A copy is saved at \(result["backup"]?.string ?? "your Orchestrator folder")."
                } else {
                    state.error = "The older service changed while you were deciding, so nothing was removed. Choose Check Again."
                }
                await state.refresh()
            } catch { state.error = error.localizedDescription }
        }
    }
    func removeBackgroundComponents() {
        let alert = NSAlert()
        alert.messageText = "Remove Orchestrator's background components?"
        alert.informativeText = "The agent stops only when idle. Your projects, settings, account connection and history are retained. You can then move the application to Trash."
        alert.addButton(withTitle: "Remove components"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            do { try await services.removeBackgroundComponents(); await state.refresh() }
            catch { state.error = error.localizedDescription }
        }
    }
    private func window<V: View>(_ key: String, title: String, view: V) {
        let window = windows[key] ?? NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 440), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = title; window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: view)
        windows[key] = window
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func showStartupError(_ message: String) { let alert = NSAlert(); alert.messageText = "Orchestrator could not start"; alert.informativeText = message; alert.runModal(); NSApplication.shared.terminate(nil) }
    @objc func reopen() { showWelcome() }
    @objc func showWelcome() { window("welcome", title: "Welcome to Orchestrator", view: WelcomeView(controller: self, state: state, services: services)) }
    @objc func showSettings() { window("settings", title: "Orchestrator Settings", view: SettingsView(controller: self, state: state, services: services)) }
    @objc func showDiagnostics() { window("diagnostics", title: "Orchestrator Diagnostics", view: DiagnosticsView(state: state)) }
    @objc func checkUpdates() { updater.check() }
    @objc func openWorkspace() { openBrowser("#/") }
    @objc func openProjects() { openBrowser("#/projects") }
    @objc func toggleRemote() { state.perform("remote_access", params: ["enabled": .bool(!(state.status?["remote_enabled"]?.bool ?? false))]) }
    @objc func quitMenu() { NSApplication.shared.terminate(nil) }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Logout and shutdown end the agent's work anyway; never block them.
        guard updater.installationPending, !updateCoordinator.mayTerminateForInstallation, !poweringOff else { return .terminateNow }
        Task {
            do { try await updateCoordinator.prepareInstallation(); sender.reply(toApplicationShouldTerminate: true) }
            catch {
                state.operation = "Orchestrator can't quit yet: an update installs when it quits, and work is still running. Your work has not been stopped."
                showSettings(); sender.reply(toApplicationShouldTerminate: false)
            }
        }
        return .terminateLater
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWelcome(); return true }
    func applicationWillTerminate(_ notification: Notification) { timer?.invalidate(); if lease >= 0 { Darwin.close(lease) }; DistributedNotificationCenter.default().removeObserver(self) }
}
