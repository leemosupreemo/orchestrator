import Foundation
import Combine

public struct UpdateJournal: Codable {
    public let schema: Int
    public let startAtLogin: Bool
    public let identity, phase: String
    public let operationID: String?
    public init(startAtLogin: Bool, identity: String, operationID: String? = nil) {
        schema = 1; self.startAtLogin = startAtLogin; self.identity = identity; self.operationID = operationID; phase = "installing"
    }
}
@MainActor public struct UpdateAdapter {
    public let prepare: () async throws -> Bool
    public let cancel: () async -> Void
    public let stop: () async throws -> Void
    public let restore: (Bool) async throws -> Void
    public let desired: () -> Bool
    public let load: () -> UpdateJournal?
    public let save: (UpdateJournal?) throws -> Void
    public init(prepare: @escaping () async throws -> Bool, cancel: @escaping () async -> Void, stop: @escaping () async throws -> Void,
                restore: @escaping (Bool) async throws -> Void, desired: @escaping () -> Bool, load: @escaping () -> UpdateJournal?, save: @escaping (UpdateJournal?) throws -> Void) {
        self.prepare = prepare; self.cancel = cancel; self.stop = stop; self.restore = restore; self.desired = desired; self.load = load; self.save = save
    }
    public static func live(client: AgentClient, services: ServiceController, operationID: @escaping () -> String? = { nil }) -> UpdateAdapter {
        UpdateAdapter(prepare: {
            let params: [String: JSONValue] = operationID().map { ["operation_id": .string($0)] } ?? [:]
            return try await client.request(command: "prepare_update", params: params).result?["accepted"]?.bool == true
        },
            cancel: { _ = try? await client.request(command: "cancel_update") }, stop: { try await services.stopPreparedForUpdate() },
            restore: { try await services.restoreAfterUpdate(startAtLogin: $0) }, desired: { services.desired }, load: {
                guard let data = UserDefaults.standard.data(forKey: "Orchestrator.desktop.updateRecovery") else { return nil }
                return try? JSONDecoder().decode(UpdateJournal.self, from: data)
            }, save: {
                if let journal = $0 { UserDefaults.standard.set(try JSONEncoder().encode(journal), forKey: "Orchestrator.desktop.updateRecovery") }
                else { UserDefaults.standard.removeObject(forKey: "Orchestrator.desktop.updateRecovery") }
            })
    }
}
@MainActor public final class UpdateCoordinator: ObservableObject {
    public enum State: String { case current, waiting, installing, failed }
    @Published public private(set) var state = State.current
    public var identity = "development"
    public var operationID: String?
    public private(set) var recoveredOperationID: String?
    public private(set) var mayTerminateForInstallation = false
    private let adapter: UpdateAdapter
    private var preparing = false
    public init(adapter: UpdateAdapter) { self.adapter = adapter }
    public func prepareInstallation() async throws {
        if mayTerminateForInstallation { return }
        guard !preparing else { throw AgentError(code: "busy", message: "Update preparation is already in progress.") }
        preparing = true
        defer { preparing = false }
        do {
            guard try await adapter.prepare() else {
                state = .waiting
                throw AgentError(code: "busy", message: "Update when current work finishes. Your work has not been stopped.")
            }
        } catch { if state != .waiting { state = .failed }; throw error }
        let journal = UpdateJournal(startAtLogin: adapter.desired(), identity: String(identity.prefix(64)), operationID: operationID)
        do {
            try adapter.save(journal)
            try await adapter.stop()
            mayTerminateForInstallation = true; state = .installing
        } catch {
            await adapter.cancel()
            do { try await adapter.restore(journal.startAtLogin); try adapter.save(nil) } catch { state = .failed }
            state = .failed
            throw error
        }
    }
    public func cancelInstallation() async {
        // Only release a gate this coordinator closed: an idle stop may hold it for its own reason.
        guard let journal = adapter.load() else {
            if mayTerminateForInstallation { await adapter.cancel() }
            mayTerminateForInstallation = false; state = .current; return
        }
        // A recovered journal retained for acknowledgement owns no gate in this process.
        if !mayTerminateForInstallation, let recoveredOperationID, journal.operationID == recoveredOperationID { return }
        mayTerminateForInstallation = false
        await adapter.cancel()
        do { try await adapter.restore(journal.startAtLogin); try adapter.save(nil); state = .current }
        catch { state = .failed }
    }
    /// True when this launch finished an update (the previous app recorded one before being replaced).
    @discardableResult public func recoverAfterRelaunch() async throws -> Bool {
        guard let journal = adapter.load() else { return false }
        guard journal.schema == 1, journal.phase == "installing", journal.identity.count <= 64 else {
            throw AgentError(code: "invalid_journal", message: "Update recovery data is invalid. Open Diagnostics.")
        }
        try await adapter.restore(journal.startAtLogin)
        // Remote completion evidence survives crashes until the agent durably accepts the result.
        if journal.operationID == nil { try adapter.save(nil) }
        recoveredOperationID = journal.operationID
        state = .current
        return true
    }
    public func acknowledgeRecovery(operationID: String) throws {
        guard recoveredOperationID == operationID, adapter.load()?.operationID == operationID else { return }
        try adapter.save(nil)
    }

}
