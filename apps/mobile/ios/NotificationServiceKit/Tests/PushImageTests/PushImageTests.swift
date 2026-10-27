import XCTest

@testable import PushImage

final class PushImageTests: XCTestCase {
    private let good = "https://mowdeck.example.org/api/v1/notifications/push-images/7a2f?e=1&s=abc"

    func testAcceptsTheOwnersHttpsPushImageUrl() {
        XCTAssertEqual(PushImage.url(from: ["image_url": good])?.absoluteString, good)
    }

    func testRejectsEverythingElse() {
        XCTAssertNil(PushImage.url(from: [:]))
        XCTAssertNil(PushImage.url(from: ["image_url": 5]))
        XCTAssertNil(PushImage.url(from: ["image_url": "http://mowdeck.example.org/api/v1/notifications/push-images/x"]))
        XCTAssertNil(PushImage.url(from: ["image_url": "https://mowdeck.example.org/other/path"]))
        XCTAssertNil(PushImage.url(from: ["image_url": "https://user:pw@mowdeck.example.org/api/v1/notifications/push-images/x"]))
        XCTAssertNil(PushImage.url(from: ["image_url": "file:///etc/passwd"]))
        XCTAssertNil(PushImage.url(from: ["image_url": good + String(repeating: "a", count: 2100)]))
    }

    func testFileExtension() {
        XCTAssertEqual(PushImage.fileExtension(forMediaType: "image/jpeg"), "jpg")
        XCTAssertEqual(PushImage.fileExtension(forMediaType: "image/PNG; charset=binary"), "png")
        XCTAssertNil(PushImage.fileExtension(forMediaType: "text/html"))
        XCTAssertNil(PushImage.fileExtension(forMediaType: nil))
    }

    func testDecisions() {
        XCTAssertEqual(PushImage.decide(statusCode: 200, mediaType: "image/jpeg", byteCount: 1000), .attach(fileExtension: "jpg"))
        XCTAssertEqual(PushImage.decide(statusCode: 200, mediaType: "image/jpeg", byteCount: 0), .giveUp)
        XCTAssertEqual(PushImage.decide(statusCode: 200, mediaType: "image/jpeg", byteCount: PushImage.maxBytes + 1), .giveUp)
        XCTAssertEqual(PushImage.decide(statusCode: 200, mediaType: "text/html", byteCount: 10), .giveUp)
        XCTAssertEqual(PushImage.decide(statusCode: 404, mediaType: nil, byteCount: 0), .retry)
        XCTAssertEqual(PushImage.decide(statusCode: 429, mediaType: nil, byteCount: 0), .retry)
        XCTAssertEqual(PushImage.decide(statusCode: 503, mediaType: nil, byteCount: 0), .retry)
        XCTAssertEqual(PushImage.decide(statusCode: 410, mediaType: nil, byteCount: 0), .giveUp)
        XCTAssertEqual(PushImage.decide(statusCode: 401, mediaType: nil, byteCount: 0), .giveUp)
    }

    func testRetryBudget() {
        XCTAssertTrue(PushImage.mayRetry(elapsed: 0))
        XCTAssertTrue(PushImage.mayRetry(elapsed: 12))
        XCTAssertFalse(PushImage.mayRetry(elapsed: 13.5))
        XCTAssertFalse(PushImage.mayRetry(elapsed: 30))
    }
}
