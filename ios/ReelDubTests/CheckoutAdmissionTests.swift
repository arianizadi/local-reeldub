import XCTest
@testable import ReelDub

@MainActor
final class CheckoutAdmissionTests: XCTestCase {
    func testPaymentSheetCannotOpenWithoutMatchingCommittedIntent() async throws {
        let account = UUID()
        let product = CloudProduct.pack.id
        var paymentSheets = 0
        for response in [nil, Data(), try body(account: UUID(), product: product), try body(account: account, product: CloudProduct.creator.id)] as [Data?] {
            do {
                _ = try await PurchaseIntent.checkout(for: product, account: account) {
                    guard let response else { throw URLError(.notConnectedToInternet) }
                    return response
                } pay: { paymentSheets += 1 }
                XCTFail("Expected unavailable admission")
            } catch {}
        }
        XCTAssertEqual(paymentSheets, 0)
        _ = try await PurchaseIntent.checkout(for: product, account: account) {
            try self.body(account: account, product: product)
        } pay: { paymentSheets += 1 }
        XCTAssertEqual(paymentSheets, 1)
    }

    @MainActor func testPurchaseDeliveryRetriesTransientFailuresOnly() {
        for status in [408, 425, 429, 500, 503] {
            XCTAssertTrue(Studio.shouldRetryPurchaseDelivery(ServiceError(status: status, errorDescription: nil)))
        }
        for status in [400, 401, 403, 409, 422] {
            XCTAssertFalse(Studio.shouldRetryPurchaseDelivery(ServiceError(status: status, errorDescription: nil)))
        }
        XCTAssertTrue(Studio.shouldRetryPurchaseDelivery(URLError(.notConnectedToInternet)))
        XCTAssertFalse(Studio.shouldRetryPurchaseDelivery(SessionError.expired))
        XCTAssertFalse(Studio.shouldRetryPurchaseDelivery(URLError(.cancelled)))
    }

    func testOnlyBalanceDenialsOfferPlans() throws {
        let url = URL(string: "https://example.org")!
        let connection = Connection(base: url, token: "test")
        for (code, reason, expected) in [(429, "daily_allowance_used", true), (429, "", false),
                                         (402, "", true), (428, "", false), (503, "", false)] {
            let response = HTTPURLResponse(url: url, statusCode: code, httpVersion: nil,
                                           headerFields: ["X-ReelDub-Reason": reason])!
            XCTAssertThrowsError(try API(connection: connection).checked(Data(), response)) { error in
                XCTAssertEqual((error as? ServiceError)?.needsBalance, expected)
            }
        }
    }

    private func body(account: UUID, product: String) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["account":account.uuidString,"product":product,"id":UUID().uuidString])
    }
}
