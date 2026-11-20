import XCTest
@testable import QualificationCore

/// Truth table for the seek landing repair. It covers the two defects the
/// judgment named: the completion flag never proves a position, and the unlimited
/// seek tolerance contradicted the strict three second landing test.
final class SeekValidatorTests: XCTestCase {
    private let validator = SeekValidator()

    func testLandingNearTargetIsAcceptedEvenWhenEngineSaysUnfinished() {
        let verdict = validator.evaluate(SeekLanding(landed: 98.0, finished: false), target: 100,
                                         duration: 1_800)
        XCTAssertEqual(verdict, .accepted(landed: 98.0))
    }

    func testFinishedFarLandingIsRejectedAndOffersOneBoundedRetry() {
        let verdict = validator.evaluate(SeekLanding(landed: 120.0, finished: true), target: 100,
                                         duration: 1_800)
        XCTAssertEqual(verdict, .retry(tolerance: .bounded, attempt: 2, reason: .farFromTarget))
    }

    func testSecondAttemptFarLandingIsARecoverableFailure() {
        let verdict = validator.evaluate(SeekLanding(landed: 120.0, finished: true), target: 100,
                                         duration: 1_800, attempt: 2)
        XCTAssertEqual(verdict, .failed(.farFromTarget(distance: 20.0)))
    }

    func testNonFiniteLandingIsRejectedEvenWhenFinished() {
        let nan = validator.evaluate(SeekLanding(landed: .nan, finished: true), target: 100,
                                     duration: 1_800)
        XCTAssertEqual(nan, .retry(tolerance: .bounded, attempt: 2, reason: .nonFiniteLanding))

        let infinite = validator.evaluate(SeekLanding(landed: .infinity, finished: true), target: 100,
                                          duration: 1_800, attempt: 2)
        XCTAssertEqual(infinite, .failed(.nonFiniteLanding))
    }

    func testMissingLandingIsTreatedAsNonFinite() {
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: nil, finished: true), target: 100,
                                          duration: 1_800),
                       .retry(tolerance: .bounded, attempt: 2, reason: .nonFiniteLanding))
    }

    func testNegativeLandingIsAbsurd() {
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: -5.0, finished: true), target: 100,
                                          duration: 1_800),
                       .retry(tolerance: .bounded, attempt: 2, reason: .absurdLanding))
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: -5.0, finished: true), target: 100,
                                          duration: 1_800, attempt: 2),
                       .failed(.absurdLanding(landed: -5.0)))
    }

    func testLandingPastTheEndOfTheItemIsAbsurd() {
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 5_000.0, finished: true), target: 30,
                                          duration: 60),
                       .retry(tolerance: .bounded, attempt: 2, reason: .absurdLanding))
    }

    func testUnlimitedToleranceLandingInsideTheWindowIsAccepted() {
        // Keyframe tolerance can land before the target; three seconds is the
        // accepted distance, exactly on the boundary included.
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 97.0, finished: true), target: 100,
                                          duration: 1_800),
                       .accepted(landed: 97.0))
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 96.9, finished: true), target: 100,
                                          duration: 1_800),
                       .retry(tolerance: .bounded, attempt: 2, reason: .farFromTarget))
    }

    func testInvalidTargetIsRejectedWithoutRetry() {
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 10, finished: true), target: .nan,
                                          duration: 1_800), .failed(.invalidTarget))
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 10, finished: true), target: -1,
                                          duration: 1_800), .failed(.invalidTarget))
    }

    func testGrowingPlaylistWithoutDurationIsNotTreatedAsAbsurd() {
        XCTAssertEqual(validator.evaluate(SeekLanding(landed: 3_600, finished: true), target: 100,
                                          duration: nil),
                       .retry(tolerance: .bounded, attempt: 2, reason: .farFromTarget))
    }

    func testFirstAttemptUsesKeyframeTolerance() {
        XCTAssertEqual(SeekValidator.firstAttemptTolerance, .keyframe)
        XCTAssertEqual(validator.tolerance, 3.0)
        XCTAssertEqual(validator.maxAttempts, 2)
    }

    func testRecoveryMessagesDoNotLeakPositionsOrURLs() {
        let failures: [SeekValidator.Failure] = [.invalidTarget, .nonFiniteLanding,
                                                 .absurdLanding(landed: 4), .farFromTarget(distance: 20)]
        for failure in failures {
            let message = SeekValidator.recoveryMessage(for: failure)
            XCTAssertFalse(message.isEmpty)
            XCTAssertFalse(message.lowercased().contains("http"))
            XCTAssertFalse(message.contains("20.0"))
        }
    }
}
