import XCTest

@testable import MowerWidgetCore

final class ModelTests: XCTestCase {
    func testDecodesTheTelemetryWidgetState() throws {
        let json = """
            {"mower_id":"7b0f4c1e-0000-4000-8000-000000000001","observed_at":"2026-10-27T12:00:00Z",
             "stale":false,"activity":"mowing","battery_percent":64,"charging":false,
             "rain_detected":null,"error_code":null,"error_message":null,
             "job":{"progress_percent":42,"area_id":"front","area_name":"Großer Garten",
                    "elapsed_s":600,"remaining_s":1800,"mowed_m2":120.5},
             "something_new":1}
            """
        let status = try JSONDecoder.mowdeck.decode(MowerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(status.activity, "mowing")
        XCTAssertEqual(status.batteryPercent, 64)
        XCTAssertEqual(status.job?.progressPercent, 42)
        XCTAssertEqual(status.job?.areaName, "Großer Garten")
        XCTAssertEqual(status.job?.remainingSeconds, 1800)
    }

    func testDecodesTheSchedulerOverviewAndToleratesAnUnknownVerdict() throws {
        let json = """
            {"mower_id":"m","at":"2026-10-27T12:00:00Z",
             "runs":[{"schedule_id":"s","name":"Morning","starts_at":"2026-10-28T08:30:00Z","duration_minutes":120}],
             "verdict":"rain_pause",
             "rain":{"raining_now":true,"expected_mm_next_hours":1.2,"wet":true,
                     "wet_until":"2026-10-27T16:00:00.250Z","pause_until":"2026-10-27T16:00:00Z","recent_mm_6h":2.0}}
            """
        let overview = try JSONDecoder.mowdeck.decode(ScheduleOverview.self, from: Data(json.utf8))
        XCTAssertEqual(overview.verdict, .rainPause)
        XCTAssertEqual(overview.runs.first?.durationMinutes, 120)
        XCTAssertEqual(overview.rain.rainingNow, true)
        XCTAssertNotNil(overview.rain.wetUntil)  // with fractional seconds
        let odd = json.replacingOccurrences(of: "rain_pause", with: "storm_of_the_century")
        let decoded = try JSONDecoder.mowdeck.decode(ScheduleOverview.self, from: Data(odd.utf8))
        XCTAssertEqual(decoded.verdict, .unsure)
    }

    func testTheLiveActivityContentStateMatchesWhatTheServerSends() throws {
        // services/notifications domain/live_activity.py content_state(): camelCase, epoch seconds.
        let json = """
            {"state":"mowing","batteryPercent":64,"progressPercent":5,"areaName":"Großer Garten",
             "remainingSeconds":1800,"endsAt":1792000000,"updatedAt":1791998200}
            """
        let state = try JSONDecoder().decode(MowingContentState.self, from: Data(json.utf8))
        XCTAssertEqual(state.state, "mowing")
        XCTAssertEqual(state.progressFraction, 0.05)
        XCTAssertEqual(state.endsAtDate, Date(timeIntervalSince1970: 1_792_000_000))
        XCTAssertFalse(state.isFinal)
        let minimal = try JSONDecoder().decode(
            MowingContentState.self,
            from: Data(#"{"state":"finished","batteryPercent":50,"updatedAt":1}"#.utf8))
        XCTAssertTrue(minimal.isFinal)
        XCTAssertNil(minimal.endsAt)
    }

    func testPushTokensAreLowerCaseHex() {
        XCTAssertEqual(PushToken.hex(Data([0x0A, 0xFF, 0x01])), "0aff01")
    }
}

final class PresentationTests: XCTestCase {
    private func status(_ activity: String, rain: Bool? = nil, stale: Bool = false) -> MowerStatus {
        MowerStatus(
            mowerId: "m", stale: stale, activity: activity, batteryPercent: 80,
            rainDetected: rain)
    }

    func testStatesFollowTheOwnersWords() {
        XCTAssertEqual(Presentation.displayState(status: status("mowing"), verdict: .runs), .mowing)
        XCTAssertEqual(Presentation.displayState(status: status("docked"), verdict: .runs), .docked)
        XCTAssertEqual(Presentation.displayState(status: status("error"), verdict: nil), .error)
        XCTAssertEqual(
            Presentation.displayState(status: status("docked"), verdict: .rainPause), .rainPause)
        XCTAssertEqual(
            Presentation.displayState(status: status("charging", rain: true), verdict: .runs),
            .rainPause)
        XCTAssertEqual(
            Presentation.displayState(status: status("charging"), verdict: .runs), .charging)
        XCTAssertEqual(
            Presentation.displayState(status: status("mowing", stale: true), verdict: .runs),
            .offline)
        XCTAssertEqual(Presentation.displayState(status: nil, verdict: nil), .unknown)
        // A rain pause never hides a job or an error.
        XCTAssertEqual(
            Presentation.displayState(status: status("mowing"), verdict: .rainPause), .mowing)
        XCTAssertEqual(
            Presentation.displayState(status: status("error"), verdict: .rainPause), .error)
    }

    func testInlineLine() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let later = now.addingTimeInterval(3600)
        let overview = ScheduleOverview(
            mowerId: "m",
            runs: [ScheduledRun(scheduleId: "s", name: "n", startsAt: later, durationMinutes: 60)],
            verdict: .runs, rain: RainFacts())
        XCTAssertEqual(
            Presentation.inlineLine(status: status("docked"), overview: overview, now: now),
            .nextRun(later))
        var rainy = overview
        rainy.verdict = .rainPause
        rainy.rain.pauseUntil = later
        XCTAssertEqual(
            Presentation.inlineLine(status: status("docked"), overview: rainy, now: now),
            .rainPause(until: later))
        XCTAssertEqual(
            Presentation.inlineLine(status: status("mowing"), overview: overview, now: now),
            .state(.mowing))
        // a run in the past is not "next"
        XCTAssertEqual(
            Presentation.inlineLine(status: status("docked"), overview: overview, now: later.addingTimeInterval(1)),
            .state(.docked))
    }

    func testRingsAndMinutes() {
        var running = status("mowing")
        running.job = JobInfo(progressPercent: 140, remainingSeconds: 61)
        XCTAssertEqual(Presentation.progressFraction(running), 1)
        XCTAssertEqual(Presentation.remainingMinutes(running), 2)
        XCTAssertEqual(Presentation.batteryFraction(status("docked")), 0.8, accuracy: 0.0001)
        XCTAssertNil(Presentation.progressFraction(status("docked")))
        XCTAssertEqual(Presentation.batteryFraction(nil), 0)
    }

    func testRefreshIsFasterWhileMowing() {
        XCTAssertLessThan(
            Presentation.refreshInterval(for: .mowing), Presentation.refreshInterval(for: .docked))
    }
}

final class CredentialTests: XCTestCase {
    func testRotationStartsThirtyDaysBeforeExpiry() {
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        func credential(expiresInDays days: Double) -> WidgetCredential {
            WidgetCredential(
                serverURL: URL(string: "https://mow.example")!, secret: "mdk_x", credentialId: "1",
                expiresAt: now.addingTimeInterval(days * 86400), deviceName: "iPhone")
        }
        XCTAssertFalse(credential(expiresInDays: 60).needsRotation(now: now))
        XCTAssertTrue(credential(expiresInDays: 29).needsRotation(now: now))
        XCTAssertFalse(credential(expiresInDays: 29).isExpired(now: now))
        XCTAssertTrue(credential(expiresInDays: -1).isExpired(now: now))
    }

    func testTheInMemoryStoreRoundTrips() throws {
        let store = InMemorySecretStore()
        XCTAssertNil(try store.load())
        let credential = WidgetCredential(
            serverURL: URL(string: "https://mow.example")!, secret: "mdk_x", credentialId: "1",
            expiresAt: Date(), deviceName: "iPhone")
        try store.save(credential)
        XCTAssertEqual(try store.load(), credential)
        try store.delete()
        XCTAssertNil(try store.load())
    }
}

/// Answers from a script and records the requests.
final class FakeTransport: HTTPTransport, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var requests: [URLRequest] = []
    var handler: (URLRequest) -> (Int, Data) = { _ in (200, Data()) }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        lock.lock()
        requests.append(request)
        let (status, data) = handler(request)
        lock.unlock()
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        return (data, response)
    }
}

final class ClientTests: XCTestCase {
    private let credential = WidgetCredential(
        serverURL: URL(string: "https://mow.example/base/")!, secret: "mdk_secret",
        credentialId: "c1", expiresAt: Date(timeIntervalSince1970: 4_000_000_000),
        deviceName: "iPhone")

    private func exchange(_ expiresIn: Int = 600, token: String = "access-1") -> Data {
        Data(#"{"access_token":"\#(token)","expires_in":\#(expiresIn)}"#.utf8)
    }

    func testExchangesTheCredentialOnceAndCachesTheAccessToken() async throws {
        let transport = FakeTransport()
        transport.handler = { request in
            if request.url!.path.hasSuffix("/identity/token") { return (200, self.exchange()) }
            return (200, Data(#"{"mower_id":"m","stale":false,"activity":"docked","battery_percent":90,"charging":true,"job":null}"#.utf8))
        }
        let client = MowDeckClient(credential: credential, transport: transport)
        _ = try await client.status(mowerId: "m")
        _ = try await client.status(mowerId: "m")
        let tokenCalls = transport.requests.filter { $0.url!.path.hasSuffix("/identity/token") }
        XCTAssertEqual(tokenCalls.count, 1)
        XCTAssertEqual(tokenCalls[0].url!.absoluteString, "https://mow.example/base/api/v1/identity/token")
        XCTAssertEqual(
            String(data: tokenCalls[0].httpBody!, encoding: .utf8), #"{"token":"mdk_secret"}"#)
        let reads = transport.requests.filter { $0.url!.path.hasSuffix("/widget") }
        XCTAssertEqual(reads.count, 2)
        XCTAssertEqual(reads[0].value(forHTTPHeaderField: "Authorization"), "Bearer access-1")
        XCTAssertEqual(reads[0].url!.path, "/base/api/v1/telemetry/mowers/m/widget")
    }

    func testRetriesOnceWithAFreshTokenOn401ThenGivesUp() async throws {
        let transport = FakeTransport()
        var exchanges = 0
        transport.handler = { request in
            if request.url!.path.hasSuffix("/identity/token") {
                exchanges += 1
                return (200, self.exchange(token: "access-\(exchanges)"))
            }
            return (401, Data())
        }
        let client = MowDeckClient(credential: credential, transport: transport)
        do {
            _ = try await client.status(mowerId: "m")
            XCTFail("expected unauthorized")
        } catch {
            XCTAssertEqual(error as? ClientError, .unauthorized)
        }
        XCTAssertEqual(exchanges, 2)
    }

    func testARejectedCredentialAndAnExpiredOneNeverReachTheApi() async {
        let transport = FakeTransport()
        transport.handler = { _ in (401, Data()) }
        let client = MowDeckClient(credential: credential, transport: transport)
        do {
            _ = try await client.mowers()
            XCTFail("expected rejection")
        } catch {
            XCTAssertEqual(error as? ClientError, .credentialRejected)
        }
        var expired = credential
        expired.expiresAt = Date(timeIntervalSince1970: 1)
        let offline = FakeTransport()
        let second = MowDeckClient(credential: expired, transport: offline)
        do {
            _ = try await second.mowers()
            XCTFail("expected rejection")
        } catch {
            XCTAssertEqual(error as? ClientError, .credentialRejected)
        }
        XCTAssertTrue(offline.requests.isEmpty)
    }

    func testStartSendsAllAreasWithTheSavedPattern() async throws {
        let transport = FakeTransport()
        transport.handler = { request in
            let path = request.url!.path
            if path.hasSuffix("/identity/token") { return (200, self.exchange()) }
            if path.hasSuffix("/map") {
                return (200, Data(#"{"map":{"areas":[{"id":"a1"},{"id":"a2"}]}}"#.utf8))
            }
            if path.hasSuffix("/commands") { return (202, Data("{}".utf8)) }
            return (200, Data(#"{"id":"m","mow_defaults":{"pattern":{"pattern":"stripes","angle_deg":45}}}"#.utf8))
        }
        let client = MowDeckClient(credential: credential, transport: transport)
        try await client.start(mowerId: "m")
        let post = transport.requests.last!
        XCTAssertEqual(post.httpMethod, "POST")
        XCTAssertEqual(post.url!.path, "/base/api/v1/fleet/mowers/m/commands")
        let body = try JSONDecoder().decode([String: JSONValue].self, from: post.httpBody!)
        guard case .object(let command)? = body["command"] else { return XCTFail("no command") }
        XCTAssertEqual(command["kind"], .string("start_mow"))
        XCTAssertEqual(command["area_ids"], .array([.string("a1"), .string("a2")]))
        XCTAssertEqual(
            command["pattern"], .object(["pattern": .string("stripes"), "angle_deg": .number(45)]))
    }

    func testPauseAndDockBodiesAndForbidden() async throws {
        let transport = FakeTransport()
        transport.handler = { request in
            request.url!.path.hasSuffix("/identity/token") ? (200, self.exchange()) : (403, Data())
        }
        let client = MowDeckClient(credential: credential, transport: transport)
        do {
            try await client.pause(mowerId: "m")
            XCTFail("expected forbidden")
        } catch {
            XCTAssertEqual(error as? ClientError, .forbidden)
        }
        XCTAssertEqual(Commands.dock["end_job"], .bool(false))
        XCTAssertEqual(Commands.pause["kind"], .string("pause_mow"))
        XCTAssertEqual(
            Commands.start(areaIds: ["a"], pattern: .null)["pattern"], nil)
    }

    func testSnapshotKeepsWhatWorksAndSurfacesARejectedCredential() async throws {
        let transport = FakeTransport()
        transport.handler = { request in
            let path = request.url!.path
            if path.hasSuffix("/identity/token") { return (200, self.exchange()) }
            if path.contains("/scheduler/") { return (404, Data()) }
            return (200, Data(#"{"mower_id":"m","stale":false,"activity":"docked","battery_percent":90,"charging":true}"#.utf8))
        }
        let client = MowDeckClient(credential: credential, transport: transport)
        let snapshot = try await client.snapshot(of: MowerRef(id: "m", name: "Luba"))
        XCTAssertEqual(snapshot.status?.batteryPercent, 90)
        XCTAssertNil(snapshot.schedule)

        let rejected = FakeTransport()
        rejected.handler = { _ in (401, Data()) }
        let broken = MowDeckClient(credential: credential, transport: rejected)
        do {
            _ = try await broken.snapshot(of: MowerRef(id: "m", name: "Luba"))
            XCTFail("expected rejection")
        } catch {
            XCTAssertEqual(error as? ClientError, .credentialRejected)
        }
    }
}
