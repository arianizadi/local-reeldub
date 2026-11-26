import SwiftUI
import XCTest
@testable import ReelDub

@MainActor
final class LayoutMatrixTests: XCTestCase {
    // Content viewports include older display sizes and narrow iPad windows that
    // can't be selected as physical device profiles on current simulator runtimes.
    private let viewports: [CGSize] = [
        CGSize(width: 320, height: 590), CGSize(width: 375, height: 667),
        CGSize(width: 375, height: 812), CGSize(width: 390, height: 844),
        CGSize(width: 393, height: 852), CGSize(width: 402, height: 874),
        CGSize(width: 414, height: 896), CGSize(width: 420, height: 912),
        CGSize(width: 428, height: 926), CGSize(width: 430, height: 932),
        CGSize(width: 440, height: 956), CGSize(width: 466, height: 678),
        CGSize(width: 669, height: 951), CGSize(width: 507, height: 744),
        CGSize(width: 744, height: 1133), CGSize(width: 756, height: 1158),
        CGSize(width: 768, height: 1024), CGSize(width: 810, height: 1080),
        CGSize(width: 820, height: 1180), CGSize(width: 834, height: 1112),
        CGSize(width: 834, height: 1194), CGSize(width: 834, height: 1210),
        CGSize(width: 1024, height: 1366), CGSize(width: 1032, height: 1376)
    ]

    private func job(_ state: String) throws -> Job {
        let data = try JSONSerialization.data(withJSONObject: [
            "id": "layout", "state": state, "stage": "Translating", "reported": false,
            "url": "", "created": "2026-11-20T08:00:00Z", "title": "Layout fixture",
            "estimated_finish": "2026-11-20T09:00:00Z", "target_language": "en"
        ])
        return try JSONDecoder().decode(Job.self, from: data)
    }

    func testComposerAndProgressFitNarrowAndWideWindowsAtLargeTextSizes() throws {
        let studio = Studio(connection: nil)
        studio.account = try JSONDecoder().decode(Account.self, from: Data(
            #"{"quality_languages":{"best":["en","de","es"],"fast":["en"]},"output_languages":["en","de","es"]}"#.utf8))
        let progress = try job("running")
        let widths = Set(viewports.flatMap { [$0.width, $0.height] })
        for type in [DynamicTypeSize.large, .xxxLarge, .accessibility5] {
            let views = [AnyView(RDComposer(studio: studio, onQueued: { _ in })), AnyView(ProgressRow(job: progress))]
            for view in views {
                let host = UIHostingController(rootView: view.dynamicTypeSize(type))
                for width in widths {
                    let fit = host.sizeThatFits(in: CGSize(width: width, height: 4000))
                    XCTAssertGreaterThan(fit.height, 10)
                    XCTAssertLessThanOrEqual(fit.width, width + 1, "Content overflow at \(width) pt, \(type)")
                }
            }
        }
    }

    func testResizedPlayerLeavesSpaceForPlaybackControlsAndSharing() {
        let host = UIHostingController(rootView: DubVideoFrame(viewport: .zero))
        for portrait in viewports {
            for size in [portrait, CGSize(width: portrait.height, height: portrait.width)] {
                let viewport = CGSize(width: max(280, size.width - 32), height: size.height - 80)
                host.rootView = DubVideoFrame(viewport: viewport)
                let width = min(viewport.width, DubVideoFrame.contentWidth) - 2 * DubVideoFrame.inset
                // A vertical scroll view offers its contents their ideal height.
                let fit = host.sizeThatFits(in: CGSize(width: width, height: 4000))
                XCTAssertGreaterThan(fit.height, 80)
                XCTAssertLessThanOrEqual(fit.width, width + 1)
                let lastControlBottom = DubVideoFrame.inset + fit.height + 18 + 44 + 18 + 50
                XCTAssertLessThanOrEqual(lastControlBottom, viewport.height + 1, "Share button below \(viewport)")
            }
        }
    }
}
