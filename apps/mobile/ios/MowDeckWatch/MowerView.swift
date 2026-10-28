import MowerWidgetCore
import SwiftUI

/// One mower: state, battery, progress, error, and the commands. The emergency stop asks once
/// more before it is sent; it is never step-up protected and never needs more than seeing the
/// mower (ADR 0060).
struct MowerView: View {
    @EnvironmentObject private var model: WatchModel
    let mowerId: String
    @State private var confirmStop = false

    private var snapshot: MowerSnapshot? { model.state?.snapshot(for: mowerId) }

    var body: some View {
        let display = WatchPresentation.display(snapshot)
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                banners
                StatusSection(snapshot: snapshot, display: display)
                commands(display)
                if let outcome = model.outcome {
                    OutcomeLine(outcome: outcome)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(snapshot?.mower.name ?? "MowDeck")
        .onAppear { model.select(mowerId) }
        .confirmationDialog(
            "Stop the mower now?", isPresented: $confirmStop, titleVisibility: .visible
        ) {
            Button("Emergency stop", role: .destructive) {
                Task { await model.send(.emergencyStop, to: mowerId) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Blades and wheels stop at once.")
        }
    }

    @ViewBuilder
    private var banners: some View {
        if !model.phoneReachable {
            ConnectionBanner(kind: .phone)
        }
        if let state = model.state {
            if !state.serverReachable {
                ConnectionBanner(kind: .server)
            } else if state.isOutdated(now: Date()) {
                ConnectionBanner(kind: .outdated(state.updatedAt))
            }
        }
    }

    @ViewBuilder
    private func commands(_ display: DisplayState) -> some View {
        let busy = model.sending != nil
        let blocked = busy || !model.phoneReachable
        VStack(spacing: 6) {
            if Self.canStart(display) {
                CommandButton(
                    title: "Start", symbol: "play.fill", running: model.sending == .start,
                    disabled: blocked
                ) {
                    Task { await model.send(.start, to: mowerId) }
                }
            }
            if display == .mowing {
                CommandButton(
                    title: "Pause", symbol: "pause.fill", running: model.sending == .pause,
                    disabled: blocked
                ) {
                    Task { await model.send(.pause, to: mowerId) }
                }
            }
            if Self.canDock(display) {
                CommandButton(
                    title: "Station", symbol: "house.fill", running: model.sending == .dock,
                    disabled: blocked
                ) {
                    Task { await model.send(.dock, to: mowerId) }
                }
            }
            // Always offered, also when the iPhone looks unreachable: the watch then says so
            // after trying. Only a stop already on its way blocks it.
            Button(role: .destructive) {
                confirmStop = true
            } label: {
                Label {
                    Text("Emergency stop")
                } icon: {
                    if model.sending == .emergencyStop {
                        ProgressView()
                    } else {
                        Image(systemName: "stop.octagon.fill")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .tint(.red)
            .buttonStyle(.borderedProminent)
            .disabled(model.sending == .emergencyStop)
            .accessibilityHint(Text("Asks for confirmation, then stops blades and wheels."))
        }
    }

    /// Start with the saved mow defaults from the station or idle (not while a job runs: a
    /// paused job is continued or ended in the app).
    static func canStart(_ display: DisplayState) -> Bool {
        [.docked, .charging, .idle, .rainPause].contains(display)
    }

    static func canDock(_ display: DisplayState) -> Bool {
        [.mowing, .paused, .idle, .error].contains(display)
    }
}

struct CommandButton: View {
    let title: LocalizedStringKey
    let symbol: String
    let running: Bool
    let disabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label {
                Text(title)
            } icon: {
                if running {
                    ProgressView()
                } else {
                    Image(systemName: symbol)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .disabled(disabled)
    }
}

struct StatusSection: View {
    let snapshot: MowerSnapshot?
    let display: DisplayState

    var body: some View {
        let status = snapshot?.status
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                if let status {
                    Gauge(value: Presentation.batteryFraction(status)) {
                        Image(systemName: status.charging ? "bolt.fill" : "battery.75percent")
                    } currentValueLabel: {
                        Text(verbatim: "\(status.batteryPercent)")
                    }
                    .gaugeStyle(.accessoryCircularCapacity)
                    .tint(WatchPresentation.batteryTint(status.batteryPercent))
                    .accessibilityLabel(Text("Battery"))
                    .accessibilityValue(Text(Double(status.batteryPercent) / 100, format: .percent))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Label {
                        Text(LocalizedStringKey(display.localizationKey))
                    } icon: {
                        Image(systemName: display.symbolName)
                    }
                    .font(.headline)
                    .foregroundStyle(WatchPresentation.tint(display))
                    if let area = status?.job?.areaName, display.isJob {
                        Text(area)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
            if display.isJob, let progress = Presentation.progressFraction(status) {
                ProgressView(value: progress) {
                    HStack {
                        Text(progress, format: .percent.precision(.fractionLength(0)))
                        Spacer()
                        if let minutes = Presentation.remainingMinutes(status) {
                            Text("\(minutes) min left")
                        }
                    }
                    .font(.footnote)
                }
                .tint(.green)
            }
            if display == .error {
                Text(status?.errorMessage ?? status?.errorCode ?? String(localized: "state.error"))
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
            if display == .offline {
                Text("The mower has not reported for a while.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct OutcomeLine: View {
    let outcome: CommandOutcome

    var body: some View {
        Label {
            if let failure = outcome.failure {
                Text(LocalizedStringKey(failure.localizationKey))
            } else {
                Text(LocalizedStringKey("sent.\(outcome.command.rawValue)"))
            }
        } icon: {
            Image(systemName: outcome.failure == nil ? "checkmark.circle.fill" : "xmark.octagon.fill")
        }
        .font(.footnote)
        .foregroundStyle(outcome.failure == nil ? Color.green : Color.red)
        .accessibilityAddTraits(.updatesFrequently)
    }
}
