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
