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
    var proposedUSPrice: String { self == .creator ? "$19.99" : "$4.99" }
}

@MainActor
extension Studio {
    func deliver(_ result: VerificationResult<StoreKit.Transaction>, using api: API, token: UUID, intent: UUID? = nil) async throws {
        let transaction = try result.payloadValue
        guard CloudProduct.allCases.contains(where: { $0.id == transaction.productID }),
              (transaction.appAccountToken == token || (transaction.productType == .autoRenewable && transaction.appAccountToken == nil)) else { throw Problem("Sign in to the ReelDub account that made this purchase.") }
        guard transaction.environment != .xcode else { throw Problem("This Xcode test purchase has no live account value.") }
        var body = ["signed_transaction": result.jwsRepresentation]
        body["intent"] = intent?.uuidString
        _ = try await api.data("v1/store/transactions", method: "POST", body: body)
        await transaction.finish()
    }

    func observePurchases() async {
        guard let api, api.connection.isHosted, let token = account?.store_token else { return }
        // Start listening before scanning unfinished purchases. A verified transaction may arrive through both paths.
        await withTaskGroup(of: Void.self) { group in
            group.addTask { @MainActor in
                for await result in StoreKit.Transaction.updates {
                    guard !Task.isCancelled else { return }
                    if (try? await self.deliver(result, using: api, token: token)) != nil { await self.refresh() }
                }
            }
            group.addTask { @MainActor in
                for await result in StoreKit.Transaction.currentEntitlements {
                    guard !Task.isCancelled else { return }
                    if let transaction = try? result.payloadValue, transaction.productType == .autoRenewable {
                        try? await self.deliver(result, using: api, token: token)
                    }
                }
                while !Task.isCancelled {
                    var delivered = false
                    for await result in StoreKit.Transaction.unfinished {
                        guard !Task.isCancelled else { return }
                        if (try? await self.deliver(result, using: api, token: token)) != nil { delivered = true }
                    }
                    if delivered { await self.refresh() }
                    try? await Task.sleep(for: .seconds(60))
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
    private var preview: Bool { studio.account?.store_token == nil || studio.account?.creator_minutes != true }
    var body: some View {
        List {
            if preview {
                Text("US pricing preview. Purchases are turned off.").foregroundStyle(.secondary)
            }
            Section {
                HStack(alignment: .center, spacing: 14) {
                    planArt("plan-free")
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Free").font(.headline)
                        Text("5 short dubs each day").font(.subheadline.weight(.medium))
                        Text("Up to 60 seconds each, in one language.").font(.footnote).foregroundStyle(.secondary)
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
            } footer: {
                Text("Daily dubs reset at midnight UTC. Creator renews monthly until canceled through Apple. We show the cost per language before you confirm.")
            }
            if let account = studio.account, account.creator_minutes == true, account.tier != "admin" {
                Section("Your balance") {
                    LabeledContent("Monthly time", value: Job.duration(account.monthly_seconds ?? 0))
                    LabeledContent("Purchased time", value: Job.duration(account.pack_seconds ?? 0))
                }
            }
            if let message { Text(message).font(.footnote) }
            if !preview, studio.failure != nil { Text("Purchases are paused while ReelDub reconnects.").font(.footnote) }
            if !preview {
                Button("Restore purchases") { Task { await restore() } }.disabled(busy)
                Button("Manage subscription") { managing = true }
            }
            Section {
                Link("Request a refund from Apple", destination: URL(string: "https://reportaproblem.apple.com/")!)
                Link("Apple standard terms of use", destination: URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!)
                Link("Privacy", destination: Connection.hosted.appending(path: "privacy"))
            }
        }
        .navigationTitle("Plans & minutes")
        .manageSubscriptionsSheet(isPresented: $managing)
        .task(id: studio.account?.store_token) {
            products = [:]
            guard !preview else { return }
            do {
                let items = try await Product.products(for: CloudProduct.allCases.map(\.id))
                try Task.checkCancellation()
                let valid = items.filter { product in CloudProduct.allCases.contains { $0.accepts(product) } }
                products = Dictionary(uniqueKeysWithValues: valid.map { ($0.id, $0) })
                if products.isEmpty { message = "The App Store has no offers available in this storefront yet." }
            } catch { message = error.localizedDescription }
        }
    }

    private func planArt(_ name: String) -> some View {
        Image(name).resizable().aspectRatio(contentMode: .fit)
            .frame(width: 82, height: 50).accessibilityHidden(true)
    }

    private func offer(_ item: CloudProduct) -> some View {
        let product = products[item.id]
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.title).font(.headline)
                Spacer()
                let price = product?.displayPrice ?? (preview ? item.proposedUSPrice : "Unavailable")
                Text(price + (item == .creator && (product != nil || preview) ? " / month" : ""))
                    .font(.footnote.weight(.semibold)).monospacedDigit().fixedSize(horizontal: true, vertical: false)
            }
            Text(item.benefit).font(.footnote).foregroundStyle(.secondary)
            if !preview, studio.account?.purchase_preflight == true, let product, studio.account?.tier != "admin" {
                if item == .creator && studio.account?.tier == "paid" {
                    Text("Current plan").font(.caption.weight(.semibold))
                } else {
                    Button(item == .creator ? "Subscribe" : "Buy " + item.title.lowercased()) {
                        Task { await buy(product) }
                    }.disabled(busy || studio.failure != nil)
                }
            }
        }.padding(.vertical, 5)
    }

    private func buy(_ product: Product) async {
        guard !busy, let api = studio.api, let token = studio.account?.store_token else { return }
        busy = true; message = nil
        defer { busy = false }
        do {
            let (intent, outcome) = try await PurchaseIntent.checkout(for: product.id, account: token) {
                try await api.data("v1/store/purchase-intents", method: "POST", body: ["product": product.id])
            } pay: {
                try await product.purchase(options: [.appAccountToken(token)])
            }
            switch outcome {
            case .success(let result):
                do {
                    try await studio.deliver(result, using: api, token: token, intent: intent.id)
                    await studio.refresh()
                    message = "Purchase added to your account."
                } catch let error as ServiceError where error.status >= 500 {
                    message = "Apple confirmed the purchase. ReelDub will retry delivery. You can also use Restore purchases."
                } catch is URLError {
                    message = "Apple confirmed the purchase. Reconnect to sync it with ReelDub."
                }
            case .pending: message = "Awaiting Apple approval. Your purchase will appear here once approved."
            case .userCancelled: break
            @unknown default: message = "The App Store hasn’t completed this purchase."
            }
        } catch { message = error.localizedDescription }
    }

    private func restore() async {
        guard !busy, let api = studio.api, let token = studio.account?.store_token else { return }
        busy = true
        defer { busy = false }
        do {
            try await AppStore.sync()
            var delivered = Set<UInt64>()
            var requiresOtherAccount = false
            var needsRetry = false
            func restoreOne(_ result: VerificationResult<StoreKit.Transaction>) async {
                guard let tx = try? result.payloadValue else { needsRetry = true; return }
                guard CloudProduct.allCases.contains(where: { $0.id == tx.productID }), !delivered.contains(tx.id) else { return }
                guard tx.appAccountToken == token || (tx.productType == .autoRenewable && tx.appAccountToken == nil) else { requiresOtherAccount = true; return }
                do {
                    try await studio.deliver(result, using: api, token: token)
                    delivered.insert(tx.id)
                } catch { needsRetry = true }
            }
            for await result in StoreKit.Transaction.currentEntitlements { await restoreOne(result) }
            for await result in StoreKit.Transaction.unfinished { await restoreOne(result) }
            let profile = try JSONDecoder().decode(Account.self, from: await api.data("v1/me"))
            guard studio.connection?.token == api.connection.token else { return }
            studio.account = profile
            if needsRetry { message = "Some purchases still need to sync. Try Restore purchases again when connected." }
            else if !delivered.isEmpty { message = "Purchases restored. Your balance is up to date." }
            else if requiresOtherAccount { message = "Sign in to the ReelDub account used for this purchase." }
            else { message = "No additional purchases to restore. Your saved minute balances are shown above." }
        } catch { message = error.localizedDescription }
    }
}
