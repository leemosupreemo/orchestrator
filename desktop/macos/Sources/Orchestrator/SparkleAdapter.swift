import AppKit
@preconcurrency import Sparkle
import DesktopCore

// Once Sparkle reaches the installing stage or shows "ready to install", its installer is armed and
// replaces the bundle when the app terminates, even after Dismiss. So a refusal while work is running
// replies Skip (which cancels the armed installer), and a user's Dismiss reports the update as pending
// so termination is gated. Automatic installation is disabled; a relaunch hook alone does not cover install-on-quit.
@MainActor final class GatedUserDriver: SPUStandardUserDriver {
    let coordinator: UpdateCoordinator
    var displayError: (String) -> Void = { _ in }
    var armed: (Bool) -> Void = { _ in }
    init(coordinator: UpdateCoordinator) {
        self.coordinator = coordinator
        super.init(hostBundle: Bundle.main, delegate: nil)
    }
    private func guarded(installerArmed: Bool, _ reply: @escaping (SPUUserUpdateChoice) -> Void) {
        Task {
            do { try await coordinator.prepareInstallation(); reply(.install) }
            catch {
                displayError(error.localizedDescription)
                if installerArmed { armed(false); reply(.skip) } else { reply(.dismiss) }
            }
        }
    }
    private func decide(_ choice: SPUUserUpdateChoice, installerArmed: Bool, downloaded: Bool, _ reply: @escaping (SPUUserUpdateChoice) -> Void) {
        if choice == .install && downloaded { guarded(installerArmed: installerArmed, reply); return }
        if installerArmed && choice == .skip { armed(false) }
        reply(choice)
    }
    override func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void) {
        coordinator.identity = appcastItem.versionString
        let installerArmed = state.stage == .installing, downloaded = state.stage != .notDownloaded
        if installerArmed { armed(true) }
        // The standard driver replies on the main thread.
        super.showUpdateFound(with: appcastItem, state: state) { [weak self] choice in
            MainActor.assumeIsolated {
                guard let self else { reply(installerArmed ? .skip : .dismiss); return }
                self.decide(choice, installerArmed: installerArmed, downloaded: downloaded, reply)
            }
        }
    }
    override func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        armed(true)
        super.showReady(toInstallAndRelaunch: { [weak self] choice in
            MainActor.assumeIsolated {
                guard let self else { reply(.skip); return }
                self.decide(choice, installerArmed: true, downloaded: true, reply)
            }
        })
    }
}

// Main-actor isolated; Sparkle's standard driver only calls it, and its replies, on the main thread.
extension GatedUserDriver: @unchecked Sendable {}

@MainActor final class SparkleAdapter: NSObject, @preconcurrency SPUUpdaterDelegate {
    let coordinator: UpdateCoordinator
    let scheduler: UpdateScheduler
    private var updater: SPUUpdater?
    private var driver: GatedUserDriver?
    private(set) var installationPending = false
    var displayError: (String) -> Void = { _ in }
    init(coordinator: UpdateCoordinator, scheduler: UpdateScheduler) { self.coordinator = coordinator; self.scheduler = scheduler; super.init() }
    private var configured: Bool {
        guard let feed = Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String, let url = URL(string: feed), url.scheme == "https", url.host != nil,
              let key = Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String, Data(base64Encoded: key)?.count == 32 else { return false }
        return true
    }
    /// Starts the updater at launch so scheduled checks run. A development build without a feed or key does nothing.
    @discardableResult func start() -> Bool {
        if updater != nil { return true }
        guard configured else { return false }
        let driver = GatedUserDriver(coordinator: coordinator)
        driver.displayError = { [weak self] in self?.displayError($0) }
        driver.armed = { [weak self] in self?.installationPending = $0 }
        let updater = SPUUpdater(hostBundle: Bundle.main, applicationBundle: Bundle.main, userDriver: driver, delegate: self)
        do { try updater.start() } catch { displayError("The signed updater could not start."); return false }
        self.driver = driver; self.updater = updater
        applyAutomatic()
        return true
    }
    /// Automatic updates check every six hours and download silently; installing waits for the scheduler.
    func applyAutomatic() {
        guard let updater else { return }
        updater.updateCheckInterval = UpdateScheduler.checkInterval
        updater.automaticallyChecksForUpdates = scheduler.enabled
        updater.automaticallyDownloadsUpdates = scheduler.enabled
    }
    func check() {
        guard start() else { displayError("Signed app updates are not configured for this development build."); return }
        updater?.checkForUpdates()
    }
    /// The hosted app asked this Mac to update: look now, download silently, install once no work is running.
    func requestUpdate() -> Bool {
        guard start(), let updater else { return false }
        scheduler.request()
        if !updater.sessionInProgress { updater.checkForUpdatesInBackground() }
        return true
    }
    func updater(_ updater: SPUUpdater, mayPerform updateCheck: SPUUpdateCheck) throws {
        if updateCheck == .updates || scheduler.enabled || scheduler.requested { return }
        throw AgentError(code: "automatic_updates_disabled", message: "Automatic updates are turned off.")
    }
    func updater(_ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem, untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        if coordinator.mayTerminateForInstallation { return false }
        Task {
            do { try await coordinator.prepareInstallation(); installHandler() }
            catch { displayError(error.localizedDescription) }
        }
        return true
    }
    /// A silently downloaded update. Sparkle would install it whenever the app quits; instead the scheduler installs it
    /// (with relaunch) once the agent is idle, and quitting meanwhile is gated like any armed installer.
    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        installationPending = true
        coordinator.identity = item.versionString
        scheduler.updateDownloaded(install: immediateInstallHandler)
        return true
    }
    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        installationPending = false
        scheduler.cancel()
        Task { await coordinator.cancelInstallation(); displayError("Update did not finish. The previous background mode has been restored.") }
    }
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        guard error != nil else { return }
        if !scheduler.waiting { installationPending = false; scheduler.cancel() }
        Task { await coordinator.cancelInstallation() }
    }
}
