import XCTest
@testable import QualificationCore

/// Language, role, Off, carry and stale-default cases for track selection.
final class TrackSelectorTests: XCTestCase {
    private let selector = TrackSelector()

    private var audioTracks: [EngineTrack] {
        [
            EngineTrack(id: "2", kind: .audio, language: "en", name: "English stereo", isDefault: true),
            EngineTrack(id: "3", kind: .audio, language: "da", name: "Dansk"),
            EngineTrack(id: "4", kind: .audio, language: "en", name: "English 5.1"),
        ]
    }

    private var subtitleTracks: [EngineTrack] {
        [
            EngineTrack(id: "5", kind: .subtitle, language: "en", name: "English full"),
            EngineTrack(id: "6", kind: .subtitle, language: "en", name: "English forced", isForced: true),
            EngineTrack(id: "7", kind: .subtitle, language: "en", name: "English SDH", isSDH: true),
            EngineTrack(id: "8", kind: .subtitle, language: "da", name: "Dansk"),
        ]
    }

    func testLanguageMapsToContainerIdentity() {
        XCTAssertEqual(selector.select(from: audioTracks, kind: .audio, preference: .language("da")),
                       .selected(audioTracks[1]))
    }

    func testExactTagBeatsPrimaryLanguage() {
        let tracks = [
            EngineTrack(id: "a", kind: .audio, language: "en-gb", name: "English UK"),
            EngineTrack(id: "b", kind: .audio, language: "en", name: "English"),
        ]
        XCTAssertEqual(selector.select(from: tracks, kind: .audio, preference: .language("en-GB")),
                       .selected(tracks[0]))
        XCTAssertEqual(selector.select(from: tracks, kind: .audio, preference: .language("en")),
                       .selected(tracks[1]), "the exact tag outranks a regional variant")
    }

    func testForcedRolePrefersTheForcedTrack() {
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle,
                                       preference: .language("en", role: .forced)),
                       .selected(subtitleTracks[1]))
    }

    func testSDHRolePrefersTheSDHTrack() {
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle,
                                       preference: .language("en", role: .sdh)),
                       .selected(subtitleTracks[2]))
    }

    func testPlainLanguagePreferenceDemotesForcedAndSDHTracks() {
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle, preference: .language("en")),
                       .selected(subtitleTracks[0]))
    }

    func testOffIsExplicitAndDoesNotFallBack() {
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle, preference: .off), .off)
    }

    func testAutomaticDefersToTheContainerDefault() {
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle, preference: .automatic), .none)
    }

    func testMissingLanguageIsNoOpinionRatherThanAWrongTrack() {
        XCTAssertEqual(selector.select(from: audioTracks, kind: .audio, preference: .language("fr")), .none)
        XCTAssertEqual(selector.select(from: subtitleTracks, kind: .subtitle, preference: .language("fr")), .none)
    }

    func testUndefinedTagIsIgnored() {
        XCTAssertEqual(selector.select(from: audioTracks, kind: .audio, preference: .language("und")), .none)
    }

    func testSelectionIsDeterministicForEqualRanks() {
        let tracks = [
            EngineTrack(id: "z", kind: .audio, language: "en", name: "English"),
            EngineTrack(id: "a", kind: .audio, language: "en", name: "English"),
        ]
        XCTAssertEqual(selector.select(from: tracks, kind: .audio, preference: .language("en")),
                       .selected(tracks[1]))
        XCTAssertEqual(selector.select(from: Array(tracks.reversed()), kind: .audio,
                                       preference: .language("en")),
                       .selected(tracks[1]), "input order must not change the choice")
    }

    func testRoleHintsReadContainerNames() {
        XCTAssertTrue(TrackSelector.roleHint(name: "English forced").isForced)
        XCTAssertTrue(TrackSelector.roleHint(name: "English SDH").isSDH)
        XCTAssertTrue(TrackSelector.roleHint(name: "English (Hard of Hearing)").isSDH)
        XCTAssertFalse(TrackSelector.roleHint(name: "Dansk").isForced)
        XCTAssertEqual(TrackSelector.roleHint(name: nil).isForced, false)
    }

    // MARK: - Carry and stale defaults

    func testLateDefaultCannotOverwriteANewerExplicitChoice() {
        var ledger = TrackSelectionLedger()
        ledger.beginItem(1)
        let captured = ledger.revision()
        XCTAssertTrue(ledger.mayApplyDefault(kind: .subtitle, revision: captured))

        // The viewer picks Off while the default task is still awaiting.
        XCTAssertTrue(ledger.recordExplicit(kind: .subtitle, choice: .off, itemToken: 1))
        XCTAssertFalse(ledger.mayApplyDefault(kind: .subtitle, revision: captured))
        XCTAssertEqual(ledger.carry(for: .subtitle), .off)
    }

    func testDefaultIsRejectedAfterTheItemWasReplaced() {
        var ledger = TrackSelectionLedger()
        ledger.beginItem(1)
        let captured = ledger.revision()
        ledger.beginItem(2)
        XCTAssertFalse(ledger.mayApplyDefault(kind: .audio, revision: captured))
    }

    func testChoiceCarriesAcrossEpisodes() {
        var ledger = TrackSelectionLedger()
        ledger.beginItem(1)
        XCTAssertTrue(ledger.recordExplicit(kind: .audio, choice: .track("da"), itemToken: 1))
        ledger.beginItem(2)
        XCTAssertEqual(ledger.carry(for: .audio), .track("da"))
        XCTAssertNil(ledger.carry(for: .subtitle))
        // A carried choice is still authoritative for the new item.
        XCTAssertFalse(ledger.mayApplyDefault(kind: .audio, revision: ledger.revision()))
        XCTAssertTrue(ledger.mayApplyDefault(kind: .subtitle, revision: ledger.revision()))
    }

    func testExplicitOffCarriesAcrossEpisodes() {
        var ledger = TrackSelectionLedger()
        ledger.beginItem(7)
        XCTAssertTrue(ledger.recordExplicit(kind: .subtitle, choice: .off, itemToken: 7))
        ledger.beginItem(8)
        XCTAssertEqual(ledger.carry(for: .subtitle), .off)
    }

    func testChoiceBelongingToAReplacedItemIsRefused() {
        var ledger = TrackSelectionLedger()
        ledger.beginItem(1)
        ledger.beginItem(2)
        XCTAssertFalse(ledger.recordExplicit(kind: .audio, choice: .track("da"), itemToken: 1))
        XCTAssertNil(ledger.carry(for: .audio))
    }

    func testFreshLedgerHasNoCarry() {
        let ledger = TrackSelectionLedger()
        XCTAssertNil(ledger.carry(for: .audio))
        XCTAssertNil(ledger.carry(for: .subtitle))
    }
}
