import AppIntents
import MowerWidgetCore
import WidgetKit

// The configuration and the buttons of the widgets (ADR 0059). The extension reads the device's
// widget credential from the Keychain and talks to the MowDeck API directly; the app does not
// need to run.

/// A mower the user can pick for a widget.
struct MowerEntity: AppEntity {
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Mower")
    static var defaultQuery = MowerQuery()

    var id: String
    var name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }

    var ref: MowerRef { MowerRef(id: id, name: name) }
}

struct MowerQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [MowerEntity] {
        try await all().filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [MowerEntity] { try await all() }

    func defaultResult() async -> MowerEntity? { try? await all().first }

    /// The server's list, falling back to the last one the app or a widget stored.
    private func all() async throws -> [MowerEntity] {
        let cached = SharedState.shared.mowers
        guard let credential = try? KeychainSecretStore().load() else {
            return cached.map { MowerEntity(id: $0.id, name: $0.name) }
        }
        do {
            let fresh = try await MowDeckClient(credential: credential).mowers()
            SharedState.shared.mowers = fresh
            return fresh.map { MowerEntity(id: $0.id, name: $0.name) }
        } catch {
            return cached.map { MowerEntity(id: $0.id, name: $0.name) }
        }
    }
}

struct SelectMowerIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Mower"
    static var description = IntentDescription("Choose which mower the widget shows.")

    @Parameter(title: "Mower")
    var mower: MowerEntity?
}

// MARK: Buttons (iOS 17 interactive widgets)

/// What a button did; the widget reloads either way.
enum CommandOutcomeError: Error, CustomLocalizedStringResourceConvertible {
    case notSetUp
    case notAllowed
    case failed

    var localizedStringResource: LocalizedStringResource {
        switch self {
        case .notSetUp: return "Open MowDeck once to set up the widgets."
        case .notAllowed: return "You are not allowed to control this mower."
        case .failed: return "The command could not be sent."
        }
    }
}

private func runCommand(_ body: (MowDeckClient) async throws -> Void) async throws {
    guard let credential = try? KeychainSecretStore().load() else { throw CommandOutcomeError.notSetUp }
    do {
        try await body(MowDeckClient(credential: credential))
    } catch ClientError.forbidden {
        throw CommandOutcomeError.notAllowed
    } catch ClientError.credentialRejected {
        throw CommandOutcomeError.notSetUp
    } catch {
        throw CommandOutcomeError.failed
    }
    WidgetCenter.shared.reloadAllTimelines()
}

struct StartMowingIntent: AppIntent {
    static var title: LocalizedStringResource = "Start mowing"
    static var description = IntentDescription("Starts all areas with the mower's saved mowing defaults.")

    @Parameter(title: "Mower ID")
    var mowerId: String

    init() {}
    init(mowerId: String) { self.mowerId = mowerId }

    func perform() async throws -> some IntentResult {
        try await runCommand { try await $0.start(mowerId: mowerId) }
        return .result()
    }
}

struct PauseMowingIntent: AppIntent {
    static var title: LocalizedStringResource = "Pause mowing"

    @Parameter(title: "Mower ID")
    var mowerId: String

    init() {}
    init(mowerId: String) { self.mowerId = mowerId }

    func perform() async throws -> some IntentResult {
        try await runCommand { try await $0.pause(mowerId: mowerId) }
        return .result()
    }
}

struct ReturnToStationIntent: AppIntent {
    static var title: LocalizedStringResource = "Return to station"

    @Parameter(title: "Mower ID")
    var mowerId: String

    init() {}
    init(mowerId: String) { self.mowerId = mowerId }

    func perform() async throws -> some IntentResult {
        try await runCommand { try await $0.dock(mowerId: mowerId) }
        return .result()
    }
}
