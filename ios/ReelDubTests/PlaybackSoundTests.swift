import AVFoundation
import XCTest
@testable import ReelDub

@MainActor
final class PlaybackSoundTests: XCTestCase {
    func testExplicitUnmuteRepairsSessionAndPlayerMute() throws {
        let player = AVPlayer()
        let audio = AVAudioSession.sharedInstance()
        defer { DubPlaybackAudio.release(player) }
        DubPlaybackAudio.mute(player)
        XCTAssertTrue(player.isMuted)
        try DubPlaybackAudio.unmute(player)
        XCTAssertEqual(audio.category, .playback)
        XCTAssertEqual(audio.mode, .moviePlayback)
        XCTAssertFalse(player.isMuted)

        try audio.setActive(false)
        try audio.setCategory(.ambient)
        if #available(iOS 26.0, *) { try audio.setOutputMuted(true) }
        try DubPlaybackAudio.unmute(player)
        XCTAssertEqual(audio.category, .playback)
        XCTAssertEqual(audio.mode, .moviePlayback)
        if #available(iOS 26.0, *) { XCTAssertFalse(audio.isOutputMuted) }
        XCTAssertFalse(player.isMuted)
    }

    func testMutingOrClosingOnePlayerPreservesOtherAudiblePlayer() throws {
        let first = AVPlayer(), second = AVPlayer()
        defer { DubPlaybackAudio.release(first); DubPlaybackAudio.release(second) }
        try DubPlaybackAudio.unmute(first)
        try DubPlaybackAudio.unmute(second)
        DubPlaybackAudio.mute(first)
        XCTAssertTrue(first.isMuted)
        DubPlaybackAudio.release(first)
        XCTAssertFalse(second.isMuted)
        XCTAssertEqual(AVAudioSession.sharedInstance().category, .playback)
        DubPlaybackAudio.mute(second)
        XCTAssertEqual(AVAudioSession.sharedInstance().category, .ambient)
    }
}
