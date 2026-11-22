import StoreKit
import SwiftUI

enum CloudProduct: String, CaseIterable, Identifiable {
    case creator = "creator.monthly"
    case pack = "minutes15"
    var id: String { "com.arianizadi.reeldub." + rawValue }
    var title: String { self == .creator ? "Creator" : "Minute pack" }
    var benefit: String {
        self == .creator ? "120 additional minutes per billing month and priority for your dubs." :
            "15 minutes for any project. Purchased minutes carry forward."
    }
    func accepts(_ product: Product) -> Bool {
        guard product.id == id else { return false }
        if self == .pack { return product.type == .consumable }
        guard product.type == .autoRenewable, let period = product.subscription?.subscriptionPeriod else { return false }
        return period.unit == .month && period.value == 1
    }
}

@MainActor
extension Studio {
    static func shouldRetryPurchaseDelivery(_ error: Error) -> Bool {
        if let network = error as? URLError { return network.code != .cancelled }
        guard let service = error as? ServiceError else { return false }
        return service.status >= 500 || [408, 425, 429].contains(service.status)
    }

    func deliver(_ result: VerificationResult<StoreKit.Transaction>, using api: API, token: UUID, intent: UUID? = nil) async throws {
        let transaction = try result.payloadValue
        guard CloudProduct.allCases.contains(where: { $0.id == transaction.productID }),
              (transaction.appAccountToken == token || (transaction.productType == .autoRenewable && transaction.appAccountToken == nil)) else { throw Problem("Sign in to the ReelDub account that made this purchase.") }
        guard transaction.environment != .xcode else { throw Problem("This Xcode test purchase has no live account value.") }
        var body = ["signed_transaction": result.jwsRepresentation]
        body["intent"] = intent?.uuidString
        do { _ = try await api.data("v1/store/transactions", method: "POST", body: body) }
        catch {
            if account?.store_token == token, Self.shouldRetryPurchaseDelivery(error) { unfinishedPurchases?.yield(()) }
            throw error
        }
        await transaction.finish()
    }

    func observePurchases() async {
        guard let api, api.connection.isHosted, let token = account?.store_token else { return }
        // Start listening before scanning unfinished purchases. A verified transaction may arrive through both paths.
        let (retries, trigger) = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        unfinishedPurchases = trigger
        trigger.yield(())
        defer { trigger.finish() }
        await withTaskGroup(of: Void.self) { group in
            group.addTask { @MainActor in
                for await result in StoreKit.Transaction.updates {
                    guard !Task.isCancelled else { return }
                    do {
                        try await self.deliver(result, using: api, token: token)
                        await self.refresh()
                    } catch { if Self.shouldRetryPurchaseDelivery(error) { trigger.yield(()) } }
                }
            }
            group.addTask { @MainActor in
                for await result in StoreKit.Transaction.currentEntitlements {
                    guard !Task.isCancelled else { return }
                    if let transaction = try? result.payloadValue, transaction.productType == .autoRenewable {
                        try? await self.deliver(result, using: api, token: token)
                    }
                }
                var retryDelay = 60
                for await _ in retries {
                    guard !Task.isCancelled else { return }
                    var delivered = false
                    var failed = false
                    for await result in StoreKit.Transaction.unfinished {
                        guard !Task.isCancelled else { return }
                        do {
                            try await self.deliver(result, using: api, token: token)
                            delivered = true
                        } catch { if Self.shouldRetryPurchaseDelivery(error) { failed = true } }
                    }
                    if delivered { await self.refresh() }
                    if failed {
                        do { try await Task.sleep(for: .seconds(retryDelay)) } catch { return }
                        retryDelay = min(retryDelay * 2, 600)
                        trigger.yield(())
                    } else { retryDelay = 60 }
                }
            }
            await group.waitForAll()
        }
    }
}

struct PurchaseIntent: Decodable {
    let id, account: UUID
    let product: String

    @MainActor static func checkout<T>(for product: String, account: UUID,
                                       prepare: () async throws -> Data,
                                       pay: () async throws -> T) async throws -> (Self, T) {
        let intent = try JSONDecoder().decode(Self.self, from: try await prepare())
        guard intent.product == product, intent.account == account else {
            throw Problem("Purchases are temporarily unavailable. Try again later.")
        }
        return (intent, try await pay())
    }
}

struct CloudPlanSheet: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            CloudStore(studio: studio).toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}

struct CloudStore: View {
    let studio: Studio
    @State private var products: [String: Product] = [:]
    @State private var busy = false
    @State private var message: String?
    @State private var managing = false
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var loadAttempt = 0
    @State private var catalogIssue: String?
    @State private var loadingCatalog = false
    private var canSell: Bool {
        studio.account?.store_token != nil && studio.account?.purchase_preflight == true && studio.account?.creator_minutes == true
    }
    private var catalogKey: String {
        "\(studio.account?.store_token?.uuidString ?? "")|\(canSell)|\(loadAttempt)"
    }
    var body: some View {
        List {
            if studio.failure != nil {
                Label("Reconnect to update your balance and purchase minutes.", systemImage: "wifi.exclamationmark")
                Button("Reconnect") { Task { await studio.refresh() } }
            } else if studio.account == nil {
                ProgressView("Checking account…")
            } else if !canSell {
                Text("New purchases are currently unavailable.").foregroundStyle(.secondary)
            }
            if !canSell { subscriptionControls }
            Section {
                HStack(alignment: .center, spacing: 14) {
                    planArt("plan-free")
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Free").font(.headline)
                        Text("5 short dubs each day").font(.subheadline.weight(.medium))
                        Text("Up to 60 seconds each, in one language.").font(.footnote).foregroundStyle(.secondary)
                        Text(Account.dailyResetNotice).font(.footnote).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }.padding(.vertical, 4)
                HStack(alignment: .center, spacing: 14) {
                    planArt("plan-creator")
                    offer(.creator)
                }
                HStack(alignment: .center, spacing: 14) {
                    planArt("plan-pack")
                    offer(.pack)
                }
            }
            if let account = studio.account, account.tier != "admin" {
                Section {
                    DubValueRow("Daily dubs left", value: account.dailyRemaining)
                    if account.creator_minutes == true {
                        DubValueRow("Monthly time", value: Account.minuteBalance(account.monthly_seconds))
                        DubValueRow("Purchased time", value: Account.minuteBalance(account.pack_seconds))
                    }
                } header: {
                    Text("Your balance").font(.headline)
                        .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
                }
            }
            if let catalogIssue { Text(catalogIssue).font(.footnote) }
            if loadingCatalog { ProgressView("Loading App Store prices…") }
            if canSell && !loadingCatalog && products.count < CloudProduct.allCases.count {
                Button("Reload App Store offers") { loadAttempt += 1 }
            }
            if canSell { subscriptionControls }
            Section {
                Link("Request a refund from Apple", destination: URL(string: "https://reportaproblem.apple.com/")!)
                Link("Apple standard terms of use", destination: URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!)
                Link("Privacy", destination: Connection.hosted.appending(path: "privacy"))
            }
        }
        .navigationTitle(typeSize.isAccessibilitySize ? "Plans" : "Plans & minutes")
        .manageSubscriptionsSheet(isPresented: $managing)
        .onChange(of: message) { _, value in
            if let value { AccessibilityNotification.Announcement(value).post() }
        }
        .task(id: catalogKey) {
            let key = catalogKey
            products = [:]
            catalogIssue = nil
            loadingCatalog = canSell
            guard canSell else { return }
            defer { if catalogKey == key { loadingCatalog = false } }
            do {
                let items = try await Product.products(for: CloudProduct.allCases.map(\.id))
                try Task.checkCancellation()
                let valid = items.filter { product in CloudProduct.allCases.contains { $0.accepts(product) } }
                products = Dictionary(uniqueKeysWithValues: valid.map { ($0.id, $0) })
                if products.isEmpty { catalogIssue = "The App Store has no offers available in this storefront yet." }
            } catch { if !Task.isCancelled { catalogIssue = "Couldn’t load App Store prices. Reload the offers to try again." } }
        }
    }

    private var subscriptionControls: some View {
        Section {
            Button("Restore purchases") { Task { await restore() } }.disabled(busy)
            Button("Manage subscription") { managing = true }
            if let message { Text(message).font(.footnote) }
        }
    }

    @ViewBuilder private func planArt(_ name: String) -> some View {
        if !typeSize.isAccessibilitySize {
            Image(name).resizable().aspectRatio(contentMode: .fit)
                .frame(width: 65, height: 48).accessibilityHidden(true)
        }
    }

    private func offer(_ item: CloudProduct) -> some View {
        let product = products[item.id]
        return VStack(alignment: .leading, spacing: 6) {
            Text(item.title).font(.headline)
            if let product {
                Text(product.displayPrice + (item == .creator ? " / month" : ""))
                    .font(.subheadline.weight(.semibold)).monospacedDigit()
            }
            Text(item.benefit).font(.footnote).foregroundStyle(.secondary)
            if item == .creator {
                Text("Renews monthly until canceled through Apple.").font(.footnote).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if canSell, let product, studio.account?.tier != "admin" {
                if item == .creator && studio.account?.tier == "paid" {
                    Text("Current plan").font(.caption.weight(.semibold))
                } else {
                    Button(item == .creator ? "Subscribe" : "Buy " + item.title.lowercased()) {
                        Task { await buy(product) }
                    }.disabled(busy || studio.failure != nil)
                    .accessibilityLabel((item == .creator ? "Subscribe to Creator for " : "Buy a minute pack for ") + product.displayPrice + (item == .creator ? " per month" : ""))
                }
            }
        }.padding(.vertical, 5)
    }

    private func buy(_ product: Product) async {
        guard !busy, canSell, studio.failure == nil, let api = studio.api, let token = studio.account?.store_token else { return }
        busy = true; message = nil
        defer { busy = false }
        do {
            let (intent, outcome) = try await PurchaseIntent.checkout(for: product.id, account: token) {
                try await api.data("v1/store/purchase-intents", method: "POST", body: ["product": product.id])
            } pay: {
                guard studio.connection?.token == api.connection.token, studio.account?.store_token == token,
                      canSell, studio.failure == nil else {
                    throw Problem("Your account changed. Reopen Plans & minutes to confirm the purchase.")
                }
                return try await product.purchase(options: [.appAccountToken(token)])
            }
            switch outcome {
            case .success(let result):
                do {
                    try await studio.deliver(result, using: api, token: token, intent: intent.id)
                    await studio.refresh()
                    message = "Purchase added to your account."
                } catch {
                    if case .verified(let transaction) = result, transaction.environment != .xcode {
                        if Studio.shouldRetryPurchaseDelivery(error) {
                            message = "Apple confirmed your purchase. Sync is pending. Stay signed in or use Restore purchases when connected."
                        } else if error is SessionError {
                            message = "Apple confirmed your purchase. Sign in again and choose Restore purchases to finish syncing."
                        } else {
                            message = "Apple confirmed your purchase, but ReelDub could not add it to this account. " + error.localizedDescription
                        }
                    } else {
                        message = error.localizedDescription
                    }
                }
            case .pending: message = "Awaiting Apple approval. Your purchase will appear here once approved."
            case .userCancelled: break
            @unknown default: message = "The App Store hasn’t completed this purchase."
            }
        } catch { message = error.localizedDescription }
    }

    private func restore() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        if studio.account?.store_token == nil || studio.failure != nil { await studio.refresh() }
        guard let api = studio.api, let token = studio.account?.store_token, studio.failure == nil else {
            message = "Purchase sync is unavailable for this account right now. Reconnect and try Restore purchases again."
            return
        }
        do {
            try await AppStore.sync()
            var delivered = Set<UInt64>()
            var requiresOtherAccount = false
            var needsRetry = false
            var blocked: String?
            func restoreOne(_ result: VerificationResult<StoreKit.Transaction>) async {
                guard let tx = try? result.payloadValue else {
                    blocked = "Apple couldn’t verify one of the purchases."
                    return
                }
                guard CloudProduct.allCases.contains(where: { $0.id == tx.productID }), !delivered.contains(tx.id) else { return }
                guard tx.appAccountToken == token || (tx.productType == .autoRenewable && tx.appAccountToken == nil) else { requiresOtherAccount = true; return }
                do {
                    try await studio.deliver(result, using: api, token: token)
                    delivered.insert(tx.id)
                } catch {
                    if Studio.shouldRetryPurchaseDelivery(error) { needsRetry = true }
                    else { blocked = error.localizedDescription }
                }
            }
            for await result in StoreKit.Transaction.currentEntitlements { await restoreOne(result) }
            for await result in StoreKit.Transaction.unfinished { await restoreOne(result) }
            let profile = try JSONDecoder().decode(Account.self, from: await api.data("v1/me"))
            guard studio.connection?.token == api.connection.token else { return }
            studio.account = profile
            if let blocked { message = "One or more purchases could not be restored. " + blocked }
            else if needsRetry { message = "Some purchases still need to sync. Try Restore purchases again when connected." }
            else if requiresOtherAccount { message = "Sign in to the ReelDub account used for this purchase." }
            else if !delivered.isEmpty { message = "Purchases restored. Your balance is up to date." }
            else { message = "No additional purchases to restore. Your saved minute balances are shown above." }
        } catch { message = error.localizedDescription }
    }
}
