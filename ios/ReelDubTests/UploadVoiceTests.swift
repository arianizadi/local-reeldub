import XCTest
@testable import ReelDub

final class UploadVoiceTests: XCTestCase {
    func testNarratorNeverTransmitsPermissionFromAPreviousSelection() throws {
        let request = try XCTUnwrap(UploadQuoteRequest(source: "clip", targets: ["es"], supported: ["es"],
            trim: false, voice: .narrator, consent: .documented, reference: "release-42", agreed: true))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(json["voice_mode"] as? String, "narrator")
        XCTAssertNil(json["voice_consent"])
        XCTAssertNil(json["consent_reference"])
    }

    func testNarratorEngineIsOptInAndScopedToAdvertisedLanguages() throws {
        func request(_ engine: NarratorEngine, target: String = "en", voice: DubVoice = .narrator) -> UploadQuoteRequest? {
            UploadQuoteRequest(source: "clip", targets: [target], supported: ["en", "es"], trim: false,
                               voice: voice, consent: .own, reference: "", agreed: true,
                               engine: engine, kittenLanguages: ["en"])
        }
        let original = try XCTUnwrap(request(.original))
        let kitten = try XCTUnwrap(request(.kitten2))
        XCTAssertNotEqual(original, kitten)
        let encoder = JSONEncoder()
        let normalPayload = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(original)) as? [String: Any])
        let testPayload = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(kitten)) as? [String: Any])
        XCTAssertNil(normalPayload["narrator_engine"])
        XCTAssertEqual(testPayload["narrator_engine"] as? String, "kitten2")
        XCTAssertNil(testPayload["voice_consent"])
        XCTAssertNil(request(.kitten2, target: "es"))
        XCTAssertNil(request(.kitten2, voice: .match))
    }

    func testMatchRequiresCurrentConsentAndAnAvailableLanguage() throws {
        func quote(_ scope: VoiceConsent?, _ ref: String = "", agreed: Bool = true, target: String = "en") -> UploadQuoteRequest? {
            UploadQuoteRequest(source: "clip", targets: [target], supported: ["en"], trim: false,
                               voice: .match, consent: scope, reference: ref, agreed: agreed)
        }
        XCTAssertNil(quote(nil))
        XCTAssertNil(quote(.own, agreed: false))
        XCTAssertNil(quote(.own, target: "xx"))
        XCTAssertNil(quote(.documented, "   "))
        XCTAssertNil(quote(.documented, String(repeating: "x", count: 161)))
        let own = try XCTUnwrap(quote(.own))
        XCTAssertNil(own.consent_reference)
        let guest = try XCTUnwrap(quote(.documented, " release-42 "))
        XCTAssertEqual(guest.consent_reference, "release-42")
        XCTAssertNotEqual(guest, own)
    }
}
