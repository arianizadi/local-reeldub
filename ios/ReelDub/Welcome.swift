import SwiftUI
import AuthenticationServices
import CryptoKit

struct Welcome: View {
    let studio: Studio
    @State private var selfHost = false
    @State private var help = false
    @State private var busy = false
    @State private var issue: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .largeTitle) private var headline = 54.0
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        GeometryReader { geo in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 9) {
                        Image("brand-mark").resizable().frame(width: 36, height: 36)
                        Text("ReelDub").font(.title3.weight(.bold))
                    }
                    .accessibilityElement(children: .combine)
                    Spacer(minLength: 28)
                    Image("voice-print").resizable().scaledToFit()
                        .frame(maxWidth: .infinity).frame(maxHeight: typeSize.isAccessibilitySize ? 46 : 160).accessibilityHidden(true)
                    Text("Reels, in your language.")
                        .font(typeSize.isAccessibilitySize ? .system(.title, design: .serif) : .system(size: headline, weight: .medium, design: .serif)).tracking(-1)
                        .fixedSize(horizontal: false, vertical: true).padding(.top, 14)
                    Text("Share a Reel from Instagram. Choose a dub language and hear it with the original background sound.")
                        .font(.body).foregroundStyle(.white.opacity(0.76)).fixedSize(horizontal: false, vertical: true).padding(.top, 12)
                    Spacer(minLength: 34)
                    VStack(alignment: .leading, spacing: 12) {
                        Label("ReelDub Cloud", systemImage: "cloud").font(.subheadline.weight(.semibold))
                        Text("Create an account or sign in to start dubbing.").font(.subheadline).foregroundStyle(.white.opacity(0.75))
                        AppleButton(studio: studio, white: true, busy: $busy, issue: $issue)
                            .frame(height: 54).disabled(busy)
                        if busy { Label("Signing in…", systemImage: "clock").font(.footnote) }
                        if let message = issue ?? studio.welcomeMessage { Text(message).font(.footnote).foregroundStyle(.white).accessibilityLabel("Sign-in message. " + message) }
                        Button("Use my own server") { selfHost = true }
                            .frame(maxWidth: .infinity, minHeight: 44).foregroundStyle(.white.opacity(0.85))
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 22) { PolicyLinks() }.fixedSize()
                            VStack(alignment: .leading, spacing: 8) { PolicyLinks() }
                        }.font(.footnote).tint(.white)
                        Button("Help & FAQ") { help = true }.font(.footnote).tint(.white).frame(minHeight: 44)
                    }
                }.padding(.horizontal, 26).padding(.vertical, 14)
                    .frame(maxWidth: 570).frame(minHeight: max(0, geo.size.height))
                    .frame(maxWidth: .infinity)
            }
        }.background(Palette.ink.ignoresSafeArea()).foregroundStyle(.white)
            .sheet(isPresented: $help) { ReelDubHelp() }
            .sheet(isPresented: $selfHost) { SelfHost(studio: studio) }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: busy)
            .onChange(of: issue) { _, value in
                if let value { AccessibilityNotification.Announcement(value).post() }
            }
    }
}

// Use Apple's own control. A tap always starts a fresh challenge or displays an error.
struct AppleButton: UIViewRepresentable {
    let studio: Studio
    let white: Bool
    @Binding var busy: Bool
    @Binding var issue: String?
    var onProof: ((String, String, String) async throws -> Void)? = nil

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> ASAuthorizationAppleIDButton {
        let button = ASAuthorizationAppleIDButton(type: .continue, style: white ? .white : .black)
        button.cornerRadius = 13
        button.accessibilityIdentifier = "continue-apple"
        button.addTarget(context.coordinator, action: #selector(Coordinator.begin(_:)), for: .touchUpInside)
        return button
    }
    func updateUIView(_ button: ASAuthorizationAppleIDButton, context: Context) {
        context.coordinator.view = self
        button.isEnabled = !busy
    }

    @MainActor final class Coordinator: NSObject, ASAuthorizationControllerDelegate, ASAuthorizationControllerPresentationContextProviding {
        var view: AppleButton
        private var flow: (URL, String)?
        private var window: UIWindow?
        private var controller: ASAuthorizationController?
        init(_ view: AppleButton) { self.view = view }

        @objc func begin(_ sender: ASAuthorizationAppleIDButton) {
            guard !view.busy, let window = sender.window else { return }
            let base = Connection.hosted
            self.window = window
            view.busy = true
            view.issue = nil
            Task {
                do {
                    let api = API(connection: Connection(base: base, token: ""))
                    let response = try await api.data("v1/apple/challenge", method: "POST")
                    let nonce = try JSONDecoder().decode(Challenge.self, from: response).challenge
                    guard sender.window != nil else { finish(nil); return }
                    flow = (base, nonce)
                    let request = ASAuthorizationAppleIDProvider().createRequest()
                    request.requestedScopes = view.onProof == nil ? [.email] : []
                    request.nonce = SHA256.hash(data: Data(nonce.utf8)).map { String(format: "%02x", $0) }.joined()
                    let auth = ASAuthorizationController(authorizationRequests: [request])
                    controller = auth
                    auth.delegate = self
                    auth.presentationContextProvider = self
                    auth.performRequests()
                } catch { finish("Couldn’t start sign-in. " + error.localizedDescription + " Tap Continue with Apple to retry.") }
            }
        }
        func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor { window! }
        func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
            guard let (base, challenge) = flow,
                  let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let data = credential.identityToken, let identity = String(data: data, encoding: .utf8) else {
                finish("Apple returned an incomplete sign-in. Please try again."); return
            }
            Task {
                do {
                    if let onProof = view.onProof {
                        let code = credential.authorizationCode.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                        try await onProof(identity, challenge, code)
                        finish(nil)
                        return
                    }
                    let api = API(connection: Connection(base: base, token: ""))
                    let payload = try await api.data("v1/apple/session", method: "POST", body: ["identity_token": identity, "challenge": challenge])
                    let session = try JSONDecoder().decode(SessionKey.self, from: payload)
                    try await view.studio.connect(Connection(base: base, token: session.token))
                    finish(nil)
                } catch { finish(error.localizedDescription) }
            }
        }
        func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
            finish((error as? ASAuthorizationError)?.code == .canceled ? nil : error.localizedDescription)
        }
        private func finish(_ message: String?) {
            controller = nil
            flow = nil
            window = nil
            view.busy = false
            view.issue = message
        }
    }
}

struct PolicyLinks: View {
    var body: some View {
        Link("Cloud privacy", destination: Connection.hosted.appending(path: "privacy")).frame(minHeight: 44)
        Link("Terms of use", destination: URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!).frame(minHeight: 44)
    }
}

struct ReelDubHelp: View {
    var suggestionAPI: API? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("Making a dub") {
                    answer("How do I translate a Reel?", "In Instagram, tap Share, choose ReelDub, pick a language and confirm. You can also paste the Reel link in ReelDub. Open Dubs to follow its progress.")
                    answer("How long can my video be?", "Choose a video up to 2 minutes long. If yours is longer, choose a shorter Reel or trim your own video before importing it.")
                    answer("Which languages can I choose?", "ReelDub detects the spoken language. The Dub language menu lists the output languages your connected server supports.")
                    answer("What if the translation sounds wrong?", DubGuidance.accuracy + " Open the dub, tap Report an issue and include the words you heard and the time they occurred.")
                    answer("How do sound and captions work?", "Tap the speaker button under the video to mute or unmute it. You can listen in Silent Mode. Use your iPhone’s volume buttons to adjust the level. Captions start on when a track is available. The subtitle switch remembers your choice.")
                }
                Section("Account and hosting") {
                    answer("How do I sign in?", "Choose Continue with Apple for ReelDub Cloud. In Account you can see how many daily dubs you have used.")
                    answer("How many free dubs do I get?", "The Cloud free plan includes five dubs per day. " + Account.dailyResetNotice + " You can see your allowance in Account.")
                    answer("Where is my finished video?", "Open Dubs to play it. Once it says Saved on this device, you can watch offline or use Share to export a copy.")
                    DisclosureGroup("Can I host ReelDub myself?") {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Yes. ReelDub is open source. Run the server on your own hardware, then choose Use my own server and enter its HTTPS address and connection key. Your server downloads and renders your dubs.")
                            Link("Open-source setup guide", destination: URL(string: "https://github.com/arianizadi/local-reeldub#readme")!)
                        }.padding(.vertical, 4)
                    }
                    answer("Where can I manage purchases?", "Open Account → Plans & minutes to see what’s available, restore eligible purchases or manage an Apple subscription.")
                }
                if let suggestionAPI {
                    Section {
                        NavigationLink("What else would you like to translate?") { VideoSuggestionForm(api: suggestionAPI) }
                    }
                }
                Section { PolicyLinks() }
            }.navigationTitle("Help & FAQ").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }

    private func answer(_ question: String, _ text: String) -> some View {
        DisclosureGroup(question) { Text(text).padding(.vertical, 4) }
    }
}
