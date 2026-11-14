// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import XCTest

@testable import MowerWidgetCore

// The area-clear confirmation of the native surfaces (ADR 0084).
final class MotionSafetyTests: XCTestCase {
    private let mowerId = "7b0f4c1e-0000-4000-8000-000000000001"

    func testStartAndResumeNeedTheConfirmation() {
        XCTAssertTrue(MotionCommandPolicy.needsAreaClearConfirmation(.start))
        XCTAssertTrue(MotionCommandPolicy.needsAreaClearConfirmation(.resume))
    }

    func testStoppingIsNeverAsked() {
        for command in [MotionCommand.pause, .returnToDock, .emergencyStop] {
            XCTAssertFalse(MotionCommandPolicy.needsAreaClearConfirmation(command), "\(command)")
        }
    }

    func testEveryCommandIsDecidedAndOnlyMotionIsAsked() {
        let asked = Set(MotionCommand.allCases.filter(MotionCommandPolicy.needsAreaClearConfirmation))
        XCTAssertEqual(asked, [.start, .resume])
        XCTAssertEqual(MotionCommand.start.rawValue, "start_mow")
        XCTAssertEqual(MotionCommand.resume.rawValue, "resume_mow")
    }

    func testTheCommandKindsMatchTheBodiesTheClientSends() {
        XCTAssertEqual(Commands.pause["kind"], .string(MotionCommand.pause.rawValue))
        XCTAssertEqual(Commands.resume["kind"], .string(MotionCommand.resume.rawValue))
        XCTAssertEqual(Commands.dock["kind"], .string(MotionCommand.returnToDock.rawValue))
        XCTAssertEqual(Commands.emergencyStop["kind"], .string(MotionCommand.emergencyStop.rawValue))
        XCTAssertEqual(
            Commands.start(areaIds: [], pattern: nil)["kind"], .string(MotionCommand.start.rawValue))
    }

    func testOnlyTheWatchStartIsAsked() {
        XCTAssertTrue(WatchCommand.start.needsAreaClearConfirmation)
        XCTAssertFalse(WatchCommand.pause.needsAreaClearConfirmation)
        XCTAssertFalse(WatchCommand.dock.needsAreaClearConfirmation)
        XCTAssertFalse(WatchCommand.emergencyStop.needsAreaClearConfirmation)
        XCTAssertEqual(WatchCommand.dock.motionCommand, .returnToDock)
    }

    // MARK: The iPhone relays a watch start only with the confirmation

    func testTheIPhoneRefusesAWatchStartWithoutTheConfirmation() {
        let unconfirmed = WatchRequest.command(.start, mowerId: mowerId).message
        XCTAssertNil(unconfirmed["areaClear"])
        XCTAssertNil(WatchRequest(message: unconfirmed), "a start the watch did not confirm")
        var forged = unconfirmed
        forged["areaClear"] = "yes"
        XCTAssertNil(WatchRequest(message: forged), "only a boolean true counts")
        let confirmed = WatchRequest.command(.start, mowerId: mowerId, areaClearConfirmed: true)
        XCTAssertEqual(confirmed.message["areaClear"] as? Bool, true)
        XCTAssertEqual(WatchRequest(message: confirmed.message), confirmed)
    }

    func testStoppingFromTheWatchNeedsNoConfirmation() {
        for command in [WatchCommand.pause, .dock, .emergencyStop] {
            let request = WatchRequest.command(command, mowerId: mowerId)
            XCTAssertEqual(WatchRequest(message: request.message), request, "\(command)")
        }
    }

    // MARK: The widget's link to the app's confirmation

    func testTheWidgetLinkRoundTrips() throws {
        let url = try XCTUnwrap(AreaClearLink.url(mowerId: mowerId))
        XCTAssertEqual(url.absoluteString, "mowdeck://mowers/\(mowerId)?confirm=start")
        let parsed = try XCTUnwrap(AreaClearLink.parse(url))
        XCTAssertEqual(parsed.mowerId, mowerId)
        XCTAssertEqual(parsed.command, .start)
        let resume = try XCTUnwrap(AreaClearLink.url(mowerId: mowerId, command: .resume))
        XCTAssertEqual(AreaClearLink.parse(resume)?.command, .resume)
    }

    func testOnlyConfirmationLinksAreTaken() {
        XCTAssertNil(AreaClearLink.url(mowerId: "../identity"), "mower ids are UUIDs")
        XCTAssertNil(AreaClearLink.url(mowerId: mowerId, command: .pause), "pause is never asked")
        for link in [
            "mowdeck://mowers/\(mowerId)",
            "mowdeck://mowers/\(mowerId)/map",
            "mowdeck://mowers/\(mowerId)?confirm=pause",
            "mowdeck://mowers/\(mowerId)?confirm=emergency_stop",
            "mowdeck://mowers/\(mowerId)?confirm=drive",
            "mowdeck://mowers/not-a-uuid?confirm=start",
            "mowdeck://auth/callback?confirm=start",
            "https://mowers/\(mowerId)?confirm=start",
        ] {
            XCTAssertNil(AreaClearLink.parse(URL(string: link)!), link)
        }
    }

    // MARK: Wording

    func testTheWordingExistsInEnglishAndGerman() throws {
        let english = try strings("en")
        let german = try strings("de")
        for key in AreaClearWording.keys {
            let en = try XCTUnwrap(english[key], "en: \(key)")
            let de = try XCTUnwrap(german[key], "de: \(key)")
            XCTAssertFalse(en.isEmpty, key)
            XCTAssertFalse(de.isEmpty, key)
        }
        XCTAssertEqual(Set(english.keys), Set(AreaClearWording.keys))
        XCTAssertEqual(Set(german.keys), Set(AreaClearWording.keys))
        XCTAssertNotEqual(english["area_clear.statement"], german["area_clear.statement"])
    }

    func testTheStatementIsTheWebAppsSafetyCheck() throws {
        // apps/web/messages/en.json: safety_title and safety_statement (PR #367).
        let english = try strings("en")
        XCTAssertEqual(english["area_clear.title"], "Before the mower moves")
        XCTAssertEqual(
            english["area_clear.statement"],
            "The area must be clear of people, children and animals. You are responsible for "
                + "supervising the mower, as its manufacturer's manual describes.")
        let german = try strings("de")
        XCTAssertEqual(german["area_clear.title"], "Bevor der Mäher fährt")
    }

    func testTheMessageNamesTheMowerAndAsks() {
        let message = AreaClearWording.message(mowerName: "Luba")
        XCTAssertTrue(message.hasPrefix("Luba\n\n"))
        XCTAssertTrue(message.contains(AreaClearWording.statement))
        XCTAssertTrue(message.hasSuffix(AreaClearWording.acknowledgement))
        XCTAssertFalse(AreaClearWording.message(mowerName: nil).hasPrefix("\n"))
        XCTAssertFalse(AreaClearWording.message(mowerName: "").hasPrefix("\n"))
        XCTAssertTrue(AreaClearWording.dialog(mowerName: nil).hasPrefix(AreaClearWording.title))
        XCTAssertNotEqual(AreaClearWording.title, "area_clear.title", "the strings are bundled")
    }

    private func strings(_ language: String) throws -> [String: String] {
        let path = try XCTUnwrap(
            AreaClearWording.bundle.path(
                forResource: "Localizable", ofType: "strings", inDirectory: nil,
                forLocalization: language),
            "\(language).lproj/Localizable.strings")
        return try XCTUnwrap(NSDictionary(contentsOfFile: path) as? [String: String])
    }
}
