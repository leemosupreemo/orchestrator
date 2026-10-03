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
        let installerArmed = state.stage == .installing
        if installerArmed { armed(true) }
        super.showUpdateFound(with: appcastItem, state: state) { [weak self] choice in
            guard let self else { reply(installerArmed ? .skip : .dismiss); return }
            self.decide(choice, installerArmed: installerArmed, downloaded: state.stage != .notDownloaded, reply)
        }
    }
    override func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        armed(true)
        super.showReady(toInstallAndRelaunch: { [weak self] choice in
            guard let self else { reply(.skip); return }
            self.decide(choice, installerArmed: true, downloaded: true, reply)
        })
    }
}

@MainActor final class SparkleAdapter: NSObject, @preconcurrency SPUUpdaterDelegate {
    let coordinator: UpdateCoordinator
    private var updater: SPUUpdater?
    private var driver: GatedUserDriver?
    private(set) var installationPending = false
    var displayError: (String) -> Void = { _ in }
    init(coordinator: UpdateCoordinator) { self.coordinator = coordinator; super.init() }
    func check() {
        guard let feed = Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String, let url = URL(string: feed), url.scheme == "https", url.host != nil,
              let key = Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String, Data(base64Encoded: key)?.count == 32 else {
            displayError("Signed app updates are not configured for this development build.")
            return
        }
        if updater == nil {
            let driver = GatedUserDriver(coordinator: coordinator)
            driver.displayError = { [weak self] in self?.displayError($0) }
            driver.armed = { [weak self] in self?.installationPending = $0 }
            let updater = SPUUpdater(hostBundle: Bundle.main, applicationBundle: Bundle.main, userDriver: driver, delegate: self)
            updater.automaticallyChecksForUpdates = false
            updater.automaticallyDownloadsUpdates = false
            do { try updater.start() } catch { displayError("The signed updater could not start."); return }
            self.driver = driver; self.updater = updater
        }
        updater?.checkForUpdates()
    }
    func updater(_ updater: SPUUpdater, mayPerform updateCheck: SPUUpdateCheck) throws {
        guard updateCheck == .updates else { throw AgentError(code: "automatic_updates_disabled", message: "Only user-initiated updates are enabled.") }
    }
    func updater(_ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem, untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        if coordinator.mayTerminateForInstallation { return false }
        Task {
            do { try await coordinator.prepareInstallation(); installHandler() }
            catch { displayError(error.localizedDescription) }
        }
        return true
    }
    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        installationPending = true
        Task {
            do { try await coordinator.prepareInstallation(); immediateInstallHandler() }
            catch { displayError(error.localizedDescription) }
        }
        return true
    }
    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        installationPending = false
        Task { await coordinator.cancelInstallation(); displayError("Update did not finish. The previous background mode has been restored.") }
    }
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        if error != nil { installationPending = false; Task { await coordinator.cancelInstallation() } }
    }
}
