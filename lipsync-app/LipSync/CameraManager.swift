import AVFoundation

/// Owns the capture session: camera permission, the video-only input,
/// front/back switching, and movie recording.
///
/// @unchecked Sendable: session state is only touched on `sessionQueue`, and
/// @Published properties are only set on the main thread.
final class CameraManager: NSObject, ObservableObject, @unchecked Sendable {
    enum Permission { case unknown, authorized, denied }

    @Published private(set) var permission: Permission = .unknown
    @Published private(set) var isRecording = false
    @Published private(set) var isUsingFrontCamera = true
    @Published var errorMessage: String?

    /// Called the moment the file output starts writing frames. Runs on the
    /// capture delegate's thread, not the main thread, so it fires as early as possible.
    var onRecordingStarted: (() -> Void)?
    /// Called when recording has stopped (for any reason).
    var onRecordingFinished: (() -> Void)?
    /// Called on the main thread with the finished silent video file.
    var onRecordingSaved: ((URL) -> Void)?

    let session = AVCaptureSession()
    private let movieOutput = AVCaptureMovieFileOutput()
    private var videoInput: AVCaptureDeviceInput?
    /// The on-screen preview, so its stabilization can match the recording's.
    private weak var previewLayer: AVCaptureVideoPreviewLayer?
    private var isConfigured = false

    // AVCaptureSession calls (configure, startRunning, start/stopRecording) block,
    // so they all run on one serial background queue, never the main thread.
    private let sessionQueue = DispatchQueue(label: "LipSync.camera.session")

    // MARK: - Permission and startup

    func start() async {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            break
        case .notDetermined:
            let granted = await AVCaptureDevice.requestAccess(for: .video)
            guard granted else { return await setPermission(.denied) }
        default: // .denied or .restricted
            return await setPermission(.denied)
        }
        await setPermission(.authorized)

        sessionQueue.async { [self] in
            if !isConfigured { configureSession() }
            if !session.isRunning { session.startRunning() }
        }
    }

    @MainActor private func setPermission(_ value: Permission) {
        permission = value
    }

    // MARK: - Session setup (runs on sessionQueue)

    private func configureSession() {
        session.beginConfiguration()
        defer { session.commitConfiguration() }

        // We manage the app's audio session ourselves (.playback in
        // AudioPlayerManager), so the capture session must not change it.
        session.automaticallyConfiguresApplicationAudioSession = false

        // Starting point only: once a camera is added, applyRecordingFormat picks
        // the exact 1080p/30 fps format, which overrides the preset.
        session.sessionPreset = session.canSetSessionPreset(.hd1920x1080) ? .hd1920x1080 : .high

        guard let input = makeInput(position: .front), session.canAddInput(input) else {
            report("Could not open the front camera.")
            return
        }
        session.addInput(input)
        videoInput = input
        applyRecordingFormat(to: input.device)

        // IMPORTANT: no audio input on purpose. The original audio file is merged
        // in later, so the speaker playback never gets picked up by the mic.

        guard session.canAddOutput(movieOutput) else {
            report("Could not set up video recording.")
            return
        }
        session.addOutput(movieOutput)
        configureVideoConnection(front: true)
        isConfigured = true
    }

    private func makeInput(position: AVCaptureDevice.Position) -> AVCaptureDeviceInput? {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position) else {
            return nil
        }
        return try? AVCaptureDeviceInput(device: device)
    }

    /// Target recording quality.
    private static let targetWidth: Int32 = 1920
    private static let targetHeight: Int32 = 1080
    private static let targetFPS: Double = 30

    /// Switches the camera to 1080p at 30 fps by picking the device format
    /// directly, so both cameras get the same format and a locked frame rate.
    /// Must run after the input is added: adding an input resets the camera
    /// to the preset's format.
    private func applyRecordingFormat(to device: AVCaptureDevice) {
        func maxFPS(_ format: AVCaptureDevice.Format) -> Double {
            format.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0
        }
        let candidates = device.formats.filter { format in
            let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            return size.width == Self.targetWidth && size.height == Self.targetHeight
        }
        // Prefer formats that reach the target frame rate, then standard 8-bit
        // video over the 10-bit HDR variants, which some apps handle badly.
        let standard = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        guard let format = candidates.max(by: { a, b in
            let aFast = maxFPS(a) >= Self.targetFPS, bFast = maxFPS(b) >= Self.targetFPS
            if aFast != bFast { return bFast }
            let aStandard = CMFormatDescriptionGetMediaSubType(a.formatDescription) == standard
            let bStandard = CMFormatDescriptionGetMediaSubType(b.formatDescription) == standard
            if aStandard != bStandard { return bStandard }
            return maxFPS(a) < maxFPS(b)
        }) else {
            report("This camera can't record in 1080p.")
            return
        }

        let fps = min(maxFPS(format), Self.targetFPS)
        do {
            try device.lockForConfiguration()
            device.activeFormat = format
            // Lock the frame rate so it doesn't drop in low light.
            let frameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
            device.activeVideoMinFrameDuration = frameDuration
            device.activeVideoMaxFrameDuration = frameDuration
            device.unlockForConfiguration()
        } catch {
            report("Could not set the camera to 1080p: \(error.localizedDescription)")
        }
    }

    /// The recording connection has to be set up again whenever the input changes.
    private func configureVideoConnection(front: Bool) {
        guard let connection = movieOutput.connection(with: .video) else { return }

        // Camera sensors are landscape. A 90° angle makes the recording portrait.
        // (On device the frames come out portrait but still carry a 90° note;
        // VideoMerger ignores that note.)
        if connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }

        // Mirror front-camera recordings so the file matches the mirrored preview,
        // the way TikTok and the iPhone Camera app do by default.
        if connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = front
        }

        // Smooth out hand shake like the Camera app, which stabilizes by default.
        // Stabilization zooms in to make room for the correction. cinematicExtended
        // zoomed in noticeably more than the Camera app (2026-11-13), so the
        // strongest mode used is .cinematic. The preview gets a matching mode below.
        let format = videoInput?.device.activeFormat
        let modes: [AVCaptureVideoStabilizationMode] = [.cinematic, .standard]
        if connection.isVideoStabilizationSupported,
           let mode = modes.first(where: { format?.isVideoStabilizationModeSupported($0) == true }) {
            connection.preferredVideoStabilizationMode = mode
        }
        applyPreviewStabilization()
    }

    /// Hands the on-screen preview to the camera so it can stabilize it too.
    func attachPreview(_ layer: AVCaptureVideoPreviewLayer) {
        sessionQueue.async { [self] in
            previewLayer = layer
            applyPreviewStabilization()
        }
    }

    /// "Preview optimized" stabilization is made for live previews: low delay,
    /// and the same field of view as the stabilized recording, so the saved
    /// video still frames exactly like the screen.
    private func applyPreviewStabilization() {
        guard let connection = previewLayer?.connection, connection.isVideoStabilizationSupported,
              videoInput?.device.activeFormat.isVideoStabilizationModeSupported(.previewOptimized) == true
        else { return }
        connection.preferredVideoStabilizationMode = .previewOptimized
    }

    // MARK: - Flip camera

    func flipCamera() {
        guard !isRecording else { return }
        sessionQueue.async { [self] in
            let toFront = !(videoInput?.device.position == .front)
            guard let newInput = makeInput(position: toFront ? .front : .back) else {
                report("That camera is not available.")
                return
            }

            session.beginConfiguration()
            if let old = videoInput { session.removeInput(old) }
            if session.canAddInput(newInput) {
                session.addInput(newInput)
                videoInput = newInput
                applyRecordingFormat(to: newInput.device)
            } else if let old = videoInput {
                session.addInput(old) // Put the old camera back if the switch fails.
            }
            let isFront = videoInput?.device.position == .front
            configureVideoConnection(front: isFront)
            session.commitConfiguration()

            DispatchQueue.main.async { self.isUsingFrontCamera = isFront }
        }
    }

    // MARK: - Recording

    func startRecording() {
        sessionQueue.async { [self] in
            guard !movieOutput.isRecording else { return }
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("recording-\(UUID().uuidString).mov")
            movieOutput.startRecording(to: url, recordingDelegate: self)
        }
    }

    func stopRecording() {
        sessionQueue.async { [self] in
            if movieOutput.isRecording { movieOutput.stopRecording() }
        }
    }

    private func report(_ message: String) {
        DispatchQueue.main.async { self.errorMessage = message }
    }
}

// MARK: - AVCaptureFileOutputRecordingDelegate

extension CameraManager: AVCaptureFileOutputRecordingDelegate {
    func fileOutput(_ output: AVCaptureFileOutput,
                    didStartRecordingTo fileURL: URL,
                    from connections: [AVCaptureConnection]) {
        // This fires when frames actually start being written, which can be a
        // little after startRecording() was called. Starting the audio here
        // rather than on the button tap keeps the audio and video lined up.
        onRecordingStarted?()
        DispatchQueue.main.async { self.isRecording = true }
    }

    func fileOutput(_ output: AVCaptureFileOutput,
                    didFinishRecordingTo outputFileURL: URL,
                    from connections: [AVCaptureConnection],
                    error: Error?) {
        onRecordingFinished?()
        DispatchQueue.main.async { self.isRecording = false }

        // An error here does not always mean failure: if recording stopped for a
        // reason like low disk space, the file may still be complete and usable.
        if let error = error as NSError? {
            let finished = error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool ?? false
            if !finished {
                report("Recording failed: \(error.localizedDescription)")
                return
            }
        }
        DispatchQueue.main.async { self.onRecordingSaved?(outputFileURL) }
    }
}
