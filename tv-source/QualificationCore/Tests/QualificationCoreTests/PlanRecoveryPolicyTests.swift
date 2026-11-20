import XCTest
@testable import QualificationCore

final class PlanRecoveryPolicyTests: XCTestCase {
    func testFirstExpiryReopensAtTheLastReliablePosition() {
        var policy = PlanExpiryRecovery()
        let action = policy.action(mediaID: "d" + String(repeating: "0", count: 31),
                                   lastReliablePosition: 612.5, keepPaused: true)
        XCTAssertEqual(action, .reopen(position: 612.5, keepPaused: true))
        XCTAssertTrue(policy.hasRecovered(mediaID: "d" + String(repeating: "0", count: 31)))
    }

    func testSecondExpiryForTheSamePassIsReportedInsteadOfReopened() {
        var policy = PlanExpiryRecovery()
        let id = String(repeating: "a", count: 32)
        _ = policy.action(mediaID: id, lastReliablePosition: 90, keepPaused: false)
        XCTAssertEqual(policy.action(mediaID: id, lastReliablePosition: 90, keepPaused: false),
                       .report(PlanExpiryRecovery.repeatedMessage))
    }

    func testAReplacementPassGetsItsOwnSingleAttempt() {
        var policy = PlanExpiryRecovery()
        _ = policy.action(mediaID: String(repeating: "a", count: 32),
                          lastReliablePosition: 30, keepPaused: false)
        let next = policy.action(mediaID: String(repeating: "b", count: 32),
                                 lastReliablePosition: 0, keepPaused: false)
        XCTAssertEqual(next, .reopen(position: 0, keepPaused: false))
    }

    func testUnknownPositionRecoversFromTheStartWithoutInventingATimestamp() {
        var policy = PlanExpiryRecovery()
        XCTAssertEqual(policy.action(mediaID: String(repeating: "c", count: 32),
                                     lastReliablePosition: nil, keepPaused: false),
                       .reopen(position: 0, keepPaused: false))
        XCTAssertEqual(policy.action(mediaID: String(repeating: "d", count: 32),
                                     lastReliablePosition: .nan, keepPaused: true),
                       .reopen(position: 0, keepPaused: true))
        XCTAssertEqual(policy.action(mediaID: String(repeating: "e", count: 32),
                                     lastReliablePosition: -12, keepPaused: false),
                       .reopen(position: 0, keepPaused: false))
    }

    func testPauseIntentIsCarriedIntoTheReopen() {
        var policy = PlanExpiryRecovery()
        XCTAssertEqual(policy.action(mediaID: String(repeating: "f", count: 32),
                                     lastReliablePosition: 45, keepPaused: true),
                       .reopen(position: 45, keepPaused: true))
    }
}
