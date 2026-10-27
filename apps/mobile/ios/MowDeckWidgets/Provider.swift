import MowerWidgetCore
import SwiftUI
import WidgetKit

/// One timeline entry: what the widget shows now.
struct MowerEntry: TimelineEntry {
    enum Problem: Equatable {
        /// No credential: the user has not enabled widgets in the app.
        case notSetUp
        /// No mower picked yet and none available.
        case noMower
        /// The server could not be reached; the last known data (if any) is shown, marked old.
        case offline
    }

    let date: Date
    let mower: MowerRef?
    let status: MowerStatus?
    let schedule: ScheduleOverview?
    /// The map picture for the light and the dark appearance (large map widget only).
    let mapLight: Data?
    let mapDark: Data?
    let problem: Problem?

    var state: DisplayState {
        Presentation.displayState(status: status, verdict: schedule?.verdict)
    }

    static func placeholder() -> MowerEntry {
        MowerEntry(
            date: Date(), mower: MowerRef(id: "preview", name: "Luba"),
            status: MowerStatus(
                mowerId: "preview", activity: "mowing", batteryPercent: 72,
                job: JobInfo(progressPercent: 42, areaName: "Garden", remainingSeconds: 2400)),
            schedule: ScheduleOverview(
                mowerId: "preview",
                runs: [
                    ScheduledRun(
                        scheduleId: "s", name: "Daily", startsAt: Date().addingTimeInterval(3 * 3600),
                        durationMinutes: 120)
                ],
                verdict: .runs, rain: RainFacts()),
            mapLight: nil, mapDark: nil, problem: nil)
    }
}

/// Loads the data of the configured mower. The extension has no session of its own: it
/// exchanges the device's widget credential for a short access token (see `MowDeckClient`).
struct MowerProvider: AppIntentTimelineProvider {
    typealias Intent = SelectMowerIntent
    typealias Entry = MowerEntry

    /// The large map widget also loads the picture.
    var withMap = false

    func placeholder(in context: Context) -> MowerEntry { .placeholder() }

    func snapshot(for configuration: SelectMowerIntent, in context: Context) async -> MowerEntry {
        if context.isPreview { return .placeholder() }
        return await load(configuration, context: context)
    }

    func timeline(for configuration: SelectMowerIntent, in context: Context) async -> Timeline<MowerEntry> {
        let entry = await load(configuration, context: context)
        let interval = entry.problem == nil
            ? Presentation.refreshInterval(for: entry.state) : 15 * 60
        return Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(interval)))
    }

    private func load(_ configuration: SelectMowerIntent, context: Context) async -> MowerEntry {
        let state = SharedState.shared
        guard let credential = try? KeychainSecretStore().load() else {
            return MowerEntry(
                date: Date(), mower: nil, status: nil, schedule: nil, mapLight: nil, mapDark: nil,
                problem: .notSetUp)
        }
        guard let mower = configuration.mower?.ref ?? state.mowers.first else {
            return MowerEntry(
                date: Date(), mower: nil, status: nil, schedule: nil, mapLight: nil, mapDark: nil,
                problem: .noMower)
        }
        let client = MowDeckClient(credential: credential)
        do {
            let snapshot = try await client.snapshot(of: mower)
            state.store(snapshot)
            var light: Data?
            var dark: Data?
            if withMap {
                let width = Int(context.displaySize.width) * 2
                let height = Int(context.displaySize.height * 0.62) * 2
                async let lightPNG = try? await client.mapPNG(
                    mowerId: mower.id, width: width, height: height, dark: false)
                async let darkPNG = try? await client.mapPNG(
                    mowerId: mower.id, width: width, height: height, dark: true)
                (light, dark) = await (lightPNG, darkPNG)
            }
            return MowerEntry(
                date: Date(), mower: mower, status: snapshot.status, schedule: snapshot.schedule,
                mapLight: light, mapDark: dark, problem: nil)
        } catch ClientError.credentialRejected, ClientError.unauthorized, ClientError.notConfigured {
            return MowerEntry(
                date: Date(), mower: mower, status: nil, schedule: nil, mapLight: nil, mapDark: nil,
                problem: .notSetUp)
        } catch {
            let last = state.snapshot(for: mower.id)
            return MowerEntry(
                date: Date(), mower: mower, status: last?.status, schedule: last?.schedule,
                mapLight: nil, mapDark: nil, problem: .offline)
        }
    }
}
