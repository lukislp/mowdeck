import XCTest

@testable import MowerWidgetCore

// The WatchConnectivity protocol of the Apple Watch app (ADR 0060).
final class WatchLinkTests: XCTestCase {
    private let mowerId = "7b0f4c1e-0000-4000-8000-000000000001"

    private func snapshot(
        _ id: String, activity: String = "docked", battery: Int = 80, progress: Int? = nil
    ) -> MowerSnapshot {
        MowerSnapshot(
            mower: MowerRef(id: id, name: "Luba"),
            status: MowerStatus(
                mowerId: id, activity: activity, batteryPercent: battery,
                job: progress.map { JobInfo(progressPercent: $0) }),
            schedule: nil,
            fetchedAt: Date(timeIntervalSince1970: 1_792_000_000))
    }

    func testCommandRequestsRoundTrip() {
        for command in WatchCommand.allCases {
            let request = WatchRequest.command(command, mowerId: mowerId)
            XCTAssertEqual(WatchRequest(message: request.message), request)
        }
        XCTAssertEqual(WatchRequest(message: WatchRequest.refresh.message), .refresh)
        XCTAssertEqual(WatchRequest.command(.emergencyStop, mowerId: mowerId).message["command"] as? String,
                       "emergency_stop")
        XCTAssertTrue(WatchCommand.emergencyStop.isEmergency)
        XCTAssertFalse(WatchCommand.start.isEmergency)
    }

    func testTheIPhoneIgnoresWhatItMustNotActOn() {
        var message = WatchRequest.command(.pause, mowerId: mowerId).message
        message["v"] = 2
        XCTAssertNil(WatchRequest(message: message), "another protocol version")
        message = WatchRequest.command(.pause, mowerId: mowerId).message
        message["command"] = "manual_drive"
        XCTAssertNil(WatchRequest(message: message), "only the four watch commands")
        message = WatchRequest.command(.pause, mowerId: mowerId).message
        message["mowerId"] = "../../identity/accounts"
        XCTAssertNil(WatchRequest(message: message), "mower ids are UUIDs, never a path")
        XCTAssertNil(WatchRequest(message: ["v": 1, "op": "delete_everything"]))
        XCTAssertNil(WatchRequest(message: [:]))
    }

    func testRepliesRoundTripAndAnUnclearAnswerIsAFailure() {
        XCTAssertTrue(WatchReply(message: WatchReply.success.message).ok)
        let refused = WatchReply(failure: .notAllowed)
        XCTAssertEqual(WatchReply(message: refused.message), refused)
        XCTAssertEqual(WatchReply(message: [:]).failure, .failed)
        XCTAssertEqual(WatchReply(message: ["ok": false, "failure": "new_reason"]).failure, .failed)
    }

    func testClientErrorsMapToWhatTheWatchSays() {
        XCTAssertEqual(WatchFailure(ClientError.credentialRejected), .notSetUp)
        XCTAssertEqual(WatchFailure(ClientError.notConfigured), .notSetUp)
        XCTAssertEqual(WatchFailure(ClientError.forbidden), .notAllowed)
        XCTAssertEqual(WatchFailure(ClientError.notFound), .notFound)
        XCTAssertEqual(WatchFailure(ClientError.server(503)), .failed)
        XCTAssertEqual(WatchFailure(URLError(.notConnectedToInternet)), .serverUnreachable)
        XCTAssertEqual(WatchFailure.serverUnreachable.localizationKey, "failure.server_unreachable")
    }

    func testStateTravelsAsApplicationContext() {
        let state = WatchState(
            configured: true, serverReachable: false,
            snapshots: [snapshot(mowerId, activity: "mowing", battery: 64, progress: 42)],
            updatedAt: Date(timeIntervalSince1970: 1_792_000_100))
        let context = state.context
        XCTAssertNotNil(context["state"] as? Data)
        XCTAssertEqual(WatchState(context: context), state)
        var other = context
        other["v"] = 99
        XCTAssertNil(WatchState(context: other))
        XCTAssertNil(WatchState(context: ["v": 1, "state": Data("{}".utf8)]))
    }

    func testStateKeepsAtMostEightMowersAndKnowsWhenItIsOld() {
        let many = (0..<12).map { snapshot("m\($0)") }
        let now = Date(timeIntervalSince1970: 1_792_000_000)
        let state = WatchState(configured: true, serverReachable: true, snapshots: many, updatedAt: now)
        XCTAssertEqual(state.snapshots.count, WatchLink.maxMowers)
        XCTAssertFalse(state.isOutdated(now: now.addingTimeInterval(14 * 60)))
        XCTAssertTrue(state.isOutdated(now: now.addingTimeInterval(16 * 60)))
        XCTAssertFalse(WatchState.notConfigured(at: now).configured)
    }

    func testComplicationTransfersOnlyForAVisibleChange() {
        let now = Date(timeIntervalSince1970: 1_792_000_000)
        func state(_ snapshots: [MowerSnapshot], configured: Bool = true) -> WatchState {
            WatchState(configured: configured, serverReachable: true, snapshots: snapshots, updatedAt: now)
        }
        let base = state([snapshot("a", activity: "mowing", battery: 64, progress: 10)])
        XCTAssertTrue(ComplicationPolicy.worthTransfer(previous: nil, current: base))
        // battery 64 -> 61 and progress 10 -> 20: same face
        XCTAssertFalse(ComplicationPolicy.worthTransfer(
            previous: base, current: state([snapshot("a", activity: "mowing", battery: 61, progress: 20)])))
        // battery step, progress step, state, mower list, configuration
        XCTAssertTrue(ComplicationPolicy.worthTransfer(
            previous: base, current: state([snapshot("a", activity: "mowing", battery: 59, progress: 10)])))
        XCTAssertTrue(ComplicationPolicy.worthTransfer(
            previous: base, current: state([snapshot("a", activity: "mowing", battery: 64, progress: 30)])))
        XCTAssertTrue(ComplicationPolicy.worthTransfer(
            previous: base, current: state([snapshot("a", activity: "error", battery: 64, progress: 10)])))
        XCTAssertTrue(ComplicationPolicy.worthTransfer(
            previous: base,
            current: state([snapshot("a", activity: "mowing", battery: 64, progress: 10), snapshot("b")])))
        XCTAssertTrue(ComplicationPolicy.worthTransfer(
            previous: base, current: state(base.snapshots, configured: false)))
    }

    func testTheWatchStoreKeepsTheStateAndThePrimaryMower() throws {
        let suite = "watch-store-tests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = WatchStore(defaults: defaults)
        XCTAssertNil(store.primarySnapshot())
        store.state = WatchState(
            configured: true, serverReachable: true, snapshots: [snapshot("a"), snapshot("b")],
            updatedAt: Date(timeIntervalSince1970: 1_792_000_000))
        XCTAssertEqual(store.primarySnapshot()?.mower.id, "a")
        store.primaryMowerId = "b"
        XCTAssertEqual(store.primarySnapshot()?.mower.id, "b")
        store.primaryMowerId = "gone"
        XCTAssertEqual(store.primarySnapshot()?.mower.id, "a")
        store.state = nil
        XCTAssertNil(store.state)
    }

    func testTheEmergencyStopBody() {
        XCTAssertEqual(Commands.emergencyStop, ["kind": .string("emergency_stop")])
    }
}
