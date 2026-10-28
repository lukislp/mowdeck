import Foundation

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionTransport: HTTPTransport {
    private let session: URLSession

    public init(session: URLSession = URLSession(configuration: .ephemeral)) {
        self.session = session
    }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ClientError.invalidResponse }
        return (data, http)
    }
}

public enum ClientError: Error, Equatable {
    /// No credential stored: widgets are not set up on this device.
    case notConfigured
    /// The credential is expired, revoked or wrong (401 from the token exchange).
    case credentialRejected
    /// The access token was not accepted even after a fresh exchange.
    case unauthorized
    /// 403: the credential lacks the right (a view-only user pressing Start).
    case forbidden
    case notFound
    /// The server answered another error status.
    case server(Int)
    case invalidResponse
}

/// The widgets' view of the MowDeck API. One instance per process; the access token is cached
/// in memory until a minute before it expires. Only the API of the credential's own server is
/// ever called (the base URL comes from the credential, never from a response).
public actor MowDeckClient {
    private let credential: WidgetCredential
    private let transport: HTTPTransport
    private let now: @Sendable () -> Date
    private var token: (value: String, expiresAt: Date)?

    public init(
        credential: WidgetCredential, transport: HTTPTransport = URLSessionTransport(),
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.credential = credential
        self.transport = transport
        self.now = now
    }

    // MARK: Requests

    private func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard var parts = URLComponents(url: credential.serverURL, resolvingAgainstBaseURL: false)
        else { throw ClientError.notConfigured }
        let base = parts.path.hasSuffix("/") ? String(parts.path.dropLast()) : parts.path
        parts.path = base + path
        parts.queryItems = query.isEmpty ? nil : query
        guard let url = parts.url else { throw ClientError.notConfigured }
        return url
    }

    func accessToken(forceRefresh: Bool = false) async throws -> String {
        if !forceRefresh, let token, token.expiresAt > now().addingTimeInterval(60) {
            return token.value
        }
        if credential.isExpired(now: now()) { throw ClientError.credentialRejected }
        var request = URLRequest(url: try url("/api/v1/identity/token"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["token": credential.secret])
        let (data, response) = try await transport.send(request)
        switch response.statusCode {
        case 200:
            struct Exchange: Decodable {
                let accessToken: String
                let expiresIn: Int
                enum CodingKeys: String, CodingKey {
                    case accessToken = "access_token"
                    case expiresIn = "expires_in"
                }
            }
            let exchange = try JSONDecoder().decode(Exchange.self, from: data)
            token = (exchange.accessToken, now().addingTimeInterval(TimeInterval(exchange.expiresIn)))
            return exchange.accessToken
        case 401, 403: throw ClientError.credentialRejected
        default: throw ClientError.server(response.statusCode)
        }
    }

    private func call(
        _ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil,
        accept: String = "application/json", retried: Bool = false
    ) async throws -> Data {
        var request = URLRequest(url: try url(path, query: query))
        request.httpMethod = method
        request.timeoutInterval = 20
        request.setValue("Bearer \(try await accessToken(forceRefresh: retried))",
                         forHTTPHeaderField: "Authorization")
        request.setValue(accept, forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await transport.send(request)
        switch response.statusCode {
        case 200..<300: return data
        case 401:
            if retried { throw ClientError.unauthorized }
            return try await call(method, path, query: query, body: body, accept: accept, retried: true)
        case 403: throw ClientError.forbidden
        case 404: throw ClientError.notFound
        default: throw ClientError.server(response.statusCode)
        }
    }

    // MARK: Reads

    public func mowers() async throws -> [MowerRef] {
        struct List: Decodable {
            struct Item: Decodable { let id: String; let name: String }
            let items: [Item]
        }
        let data = try await call("GET", "/api/v1/fleet/mowers")
        return try JSONDecoder().decode(List.self, from: data).items.map { MowerRef(id: $0.id, name: $0.name) }
    }

    public func status(mowerId: String) async throws -> MowerStatus {
        let data = try await call("GET", "/api/v1/telemetry/mowers/\(mowerId)/widget")
        return try JSONDecoder.mowdeck.decode(MowerStatus.self, from: data)
    }

    public func schedule(mowerId: String) async throws -> ScheduleOverview {
        let data = try await call("GET", "/api/v1/scheduler/mowers/\(mowerId)/widget")
        return try JSONDecoder.mowdeck.decode(ScheduleOverview.self, from: data)
    }

    /// The map picture (PNG), `GET /api/v1/telemetry/mowers/{id}/widget-map.png`.
    public func mapPNG(mowerId: String, width: Int, height: Int, dark: Bool) async throws -> Data {
        try await call(
            "GET", "/api/v1/telemetry/mowers/\(mowerId)/widget-map.png",
            query: [
                URLQueryItem(name: "width", value: String(width)),
                URLQueryItem(name: "height", value: String(height)),
                URLQueryItem(name: "theme", value: dark ? "dark" : "light"),
            ],
            accept: "image/png")
    }

    /// Status and schedule together; a part that fails stays nil, so the widget shows the rest.
    /// A rejected credential is not swallowed: the widget shows the set-up hint.
    public func snapshot(of mower: MowerRef) async throws -> MowerSnapshot {
        async let statusResult = Result { try await self.status(mowerId: mower.id) }
        async let scheduleResult = Result { try await self.schedule(mowerId: mower.id) }
        let status = await statusResult
        let schedule = await scheduleResult
        for failure in [status.failure, schedule.failure] {
            if let error = failure as? ClientError,
               error == .credentialRejected || error == .unauthorized || error == .notConfigured {
                throw error
            }
        }
        return MowerSnapshot(
            mower: mower, status: try? status.get(), schedule: try? schedule.get(),
            fetchedAt: now())
    }

    // MARK: Commands (same rights as the user; none is step-up protected)

    /// Start mowing all areas with the mower's saved mow defaults (`mow_defaults.pattern`).
    public func start(mowerId: String) async throws {
        let areas = try await areaIds(mowerId: mowerId)
        let pattern = try await savedPattern(mowerId: mowerId)
        try await send(Commands.start(areaIds: areas, pattern: pattern), to: mowerId)
    }

    public func pause(mowerId: String) async throws {
        try await send(Commands.pause, to: mowerId)
    }

    public func resume(mowerId: String) async throws {
        try await send(Commands.resume, to: mowerId)
    }

    /// Drive back to the station and keep the job so it can resume (`end_job: false`).
    public func dock(mowerId: String) async throws {
        try await send(Commands.dock, to: mowerId)
    }

    private func send(_ command: [String: JSONValue], to mowerId: String) async throws {
        let body = try JSONEncoder().encode(["command": JSONValue.object(command)])
        _ = try await call("POST", "/api/v1/fleet/mowers/\(mowerId)/commands", body: body)
    }

    private func areaIds(mowerId: String) async throws -> [String] {
        struct MapReply: Decodable {
            struct Area: Decodable { let id: String }
            struct Map: Decodable { let areas: [Area] }
            let map: Map
        }
        let data = try await call("GET", "/api/v1/telemetry/mowers/\(mowerId)/map")
        return try JSONDecoder().decode(MapReply.self, from: data).map.areas.map(\.id)
    }

    private func savedPattern(mowerId: String) async throws -> JSONValue? {
        struct Detail: Decodable {
            struct Defaults: Decodable { let pattern: JSONValue? }
            let mowDefaults: Defaults?
            enum CodingKeys: String, CodingKey { case mowDefaults = "mow_defaults" }
        }
        let data = try await call("GET", "/api/v1/fleet/mowers/\(mowerId)")
        return try JSONDecoder().decode(Detail.self, from: data).mowDefaults?.pattern
    }
}

private extension Result {
    var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }

    init(catching body: @Sendable () async throws -> Success) async where Failure == Error {
        do { self = .success(try await body()) } catch { self = .failure(error) }
    }
}

/// The command bodies (`CommandRequest.command`).
public enum Commands {
    public static func start(areaIds: [String], pattern: JSONValue?) -> [String: JSONValue] {
        var command: [String: JSONValue] = [
            "kind": .string("start_mow"),
            "area_ids": .array(areaIds.map { .string($0) }),
        ]
        if let pattern, pattern != .null { command["pattern"] = pattern }
        return command
    }

    public static let pause: [String: JSONValue] = ["kind": .string("pause_mow")]
    public static let resume: [String: JSONValue] = ["kind": .string("resume_mow")]
    public static let dock: [String: JSONValue] = [
        "kind": .string("return_to_dock"), "end_job": .bool(false),
    ]
}
