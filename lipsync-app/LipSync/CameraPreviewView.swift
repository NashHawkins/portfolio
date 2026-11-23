import AVFoundation
import SwiftUI

/// Shows the live camera feed. A UIView whose backing layer is an
/// AVCaptureVideoPreviewLayer resizes with the view automatically.
struct CameraPreviewView: UIViewRepresentable {
    let session: AVCaptureSession
    /// Reports the preview's size whenever it changes, so the saved video can
    /// be trimmed to exactly what was on screen.
    var onSizeChange: (CGSize) -> Void = { _ in }
    /// Hands over the preview layer once, so the camera can stabilize it.
    var onLayerReady: (AVCaptureVideoPreviewLayer) -> Void = { _ in }

    func makeUIView(context: Context) -> PreviewUIView {
        let view = PreviewUIView()
        view.onSizeChange = onSizeChange
        view.previewLayer.session = session
        // Show the whole 9:16 camera frame (thin bars where the screen is taller), so the preview
        // shows exactly what is recorded. Filling the screen would hide the sides of the video.
        view.previewLayer.videoGravity = .resizeAspect
        // The preview layer mirrors the front camera automatically
        // (automaticallyAdjustsVideoMirroring is on by default).
        onLayerReady(view.previewLayer)
        return view
    }

    func updateUIView(_ uiView: PreviewUIView, context: Context) {}

    final class PreviewUIView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
        var onSizeChange: (CGSize) -> Void = { _ in }
        private var lastSize: CGSize = .zero

        override func layoutSubviews() {
            super.layoutSubviews()
            guard bounds.size != lastSize else { return }
            lastSize = bounds.size
            onSizeChange(bounds.size)
        }
    }
}
