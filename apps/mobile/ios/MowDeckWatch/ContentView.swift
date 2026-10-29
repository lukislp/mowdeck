import MowerWidgetCore
import SwiftUI

/// The first screen: the one mower directly, else the list of mowers.
struct ContentView: View {
    @EnvironmentObject private var model: WatchModel

    var body: some View {
        NavigationStack {
            Group {
                if let state = model.state {
                    if !state.configured, state.signedOut == true {
                        HintView(
                            symbol: "person.crop.circle.badge.questionmark",
                            title: "Signed out",
                            message: "Open MowDeck on the iPhone to sign in.")
                    } else if !state.configured {
                        HintView(
                            symbol: "iphone.gen3",
                            title: "Set up on the iPhone",
                            message: "Open MowDeck on the iPhone and turn on widgets in the settings.")
                    } else if state.snapshots.isEmpty {
                        HintView(
                            symbol: "leaf",
                            title: "No mowers",
                            message: "Add a mower in MowDeck.")
                    } else if state.snapshots.count == 1, let only = state.snapshots.first {
                        MowerView(mowerId: only.mower.id)
                    } else {
                        MowerList(state: state)
                    }
                } else {
                    HintView(
                        symbol: "applewatch.radiowaves.left.and.right",
                        title: "Waiting for the iPhone",
                        message: "Open MowDeck on the iPhone once.")
                }
            }
            .navigationDestination(for: String.self) { mowerId in
                MowerView(mowerId: mowerId)
            }
        }
    }
}

struct MowerList: View {
    @EnvironmentObject private var model: WatchModel
    let state: WatchState

    var body: some View {
        List {
            if !model.phoneReachable {
                ConnectionBanner(kind: .phone)
            }
            ForEach(state.snapshots, id: \.mower.id) { snapshot in
                NavigationLink(value: snapshot.mower.id) {
                    MowerRow(snapshot: snapshot)
                }
            }
        }
        .navigationTitle("MowDeck")
    }
}

struct MowerRow: View {
    let snapshot: MowerSnapshot

    var body: some View {
        let display = WatchPresentation.display(snapshot)
        HStack(spacing: 8) {
            Image(systemName: display.symbolName)
                .foregroundStyle(WatchPresentation.tint(display))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(snapshot.mower.name)
                    .font(.headline)
                    .lineLimit(1)
                HStack(spacing: 4) {
                    Text(LocalizedStringKey(display.localizationKey))
                    if let battery = snapshot.status?.batteryPercent {
                        Text(verbatim: "·")
                        Text(Double(battery) / 100, format: .percent)
                    }
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct HintView: View {
    let symbol: String
    let title: LocalizedStringKey
    let message: LocalizedStringKey

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                Image(systemName: symbol)
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                Text(title)
                    .font(.headline)
                    .multilineTextAlignment(.center)
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
        }
    }
}

/// "iPhone not reachable" or "Server not reachable": the watch says plainly why it cannot act
/// or why the values may be old.
struct ConnectionBanner: View {
    enum Kind {
        case phone
        case server
        case outdated(Date)
    }

    let kind: Kind

    var body: some View {
        Label {
            switch kind {
            case .phone: Text("iPhone not reachable")
            case .server: Text("Server not reachable, last known state")
            case .outdated(let date): Text("Updated \(Text(date, style: .relative)) ago")
            }
        } icon: {
            switch kind {
            case .phone: Image(systemName: "iphone.slash")
            case .server: Image(systemName: "wifi.slash")
            case .outdated: Image(systemName: "clock.badge.exclamationmark")
            }
        }
        .font(.footnote)
        .foregroundStyle(.orange)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Words, glyphs and colours of the watch, on top of the widgets' rules (`Presentation`).
enum WatchPresentation {
    static func display(_ snapshot: MowerSnapshot?) -> DisplayState {
        Presentation.displayState(status: snapshot?.status, verdict: snapshot?.schedule?.verdict)
    }

    static func tint(_ state: DisplayState) -> Color {
        switch state {
        case .mowing: return .green
        case .paused, .returning: return .orange
        case .error: return .red
        case .rainPause: return .blue
        case .charging: return .yellow
        case .offline, .unknown: return .gray
        case .docked, .idle, .updating: return .secondary
        }
    }

    static func batteryTint(_ percent: Int) -> Color {
        if percent <= 15 { return .red }
        if percent <= 30 { return .orange }
        return .green
    }
}
