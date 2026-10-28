import SwiftUI

/// The MowDeck Apple Watch app (ADR 0060): the mower's state and the commands Start, Pause,
/// Station and the emergency stop, relayed through the iPhone app. The complication is the
/// separate extension MowDeckWatchWidgets.
@main
struct MowDeckWatchApp: App {
    @StateObject private var model = WatchModel.shared
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Before any view: state and complication data can be the reason for a background launch.
        WatchModel.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.refresh() }
        }
        .backgroundTask(.watchConnectivity) {
            await WatchModel.shared.drainPendingContent()
        }
    }
}
