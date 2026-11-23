import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var camera = CameraManager()
    @StateObject private var audio = AudioPlayerManager()

    @State private var showingAudioPicker = false
    @State private var showingQueue = false
    @State private var review: ReviewItem?

    /// mp3, m4a, wav and raw aac. `.audio` also covers anything else iOS can play.
    private let audioTypes: [UTType] = [.mp3, .mpeg4Audio, .wav, UTType("public.aac-audio"), .audio]
        .compactMap { $0 }

    private var isBusy: Bool { camera.isRecording }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            switch camera.permission {
            case .unknown:
                ProgressView().tint(.white)
            case .denied:
                PermissionDeniedView()
            case .authorized:
                cameraScreen
            }
        }
        .task {
            wireUpCallbacks()
            await camera.start()
        }
        .fileImporter(isPresented: $showingAudioPicker, allowedContentTypes: audioTypes) { result in
            switch result {
            case .success(let url): audio.importAudio(from: url)
            case .failure(let error): audio.errorMessage = error.localizedDescription
            }
        }
        .fullScreenCover(item: $review) { item in
            ReviewView(item: item) { review = nil }
        }
        .sheet(isPresented: $showingQueue) {
            QueueView()
        }
        .alert("Something went wrong", isPresented: errorBinding) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(camera.errorMessage ?? audio.errorMessage ?? "")
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { camera.errorMessage != nil || audio.errorMessage != nil },
                set: { if !$0 { camera.errorMessage = nil; audio.errorMessage = nil } })
    }

    /// Connects the camera and the audio player so they start and stop together.
    private func wireUpCallbacks() {
        camera.onRecordingStarted = { [weak audio] in audio?.play() }
        camera.onRecordingFinished = { [weak audio] in audio?.stop() }
        // When the clip ends, end the recording too (like TikTok).
        audio.onFinishedPlaying = { [weak camera] in camera?.stopRecording() }
        camera.onRecordingSaved = { url in
            review = ReviewItem(videoURL: url,
                                audioURL: audio.chosen?.url,
                                output: audio.lastOutput,
                                frameAspect: nil) // keep the camera's full 9:16 frame
        }
    }

    // MARK: - Recording flow

    private func recordButtonTapped() {
        if camera.isRecording {
            audio.stop() // Stop the sound right away instead of waiting for the file to finish.
            camera.stopRecording()
        } else {
            audio.prepare()
            camera.startRecording() // Audio starts in onRecordingStarted.
        }
    }

    // MARK: - Layout

    private var cameraScreen: some View {
        ZStack {
            CameraPreviewView(session: camera.session,
                              onLayerReady: { layer in camera.attachPreview(layer) })
                .ignoresSafeArea()

            VStack(spacing: 12) {
                AudioChoiceBar(chosen: audio.chosen) { showingAudioPicker = true }
                    .disabled(isBusy)
                    .opacity(isBusy ? 0.5 : 1)

                if camera.isRecording {
                    Label(audio.isPlaying ? "REC  ♪" : "REC", systemImage: "circle.fill")
                        .font(.headline)
                        .foregroundStyle(.red)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(.black.opacity(0.5), in: Capsule())
                }

                Spacer()

                HStack {
                    Button {
                        showingQueue = true
                    } label: {
                        Image(systemName: "tray.full")
                            .font(.title)
                            .foregroundStyle(.white)
                            .frame(width: 64, height: 64)
                            .background(.black.opacity(0.4), in: Circle())
                    }
                    .accessibilityLabel("Queued videos")
                    .disabled(isBusy)
                    .opacity(isBusy ? 0.3 : 1)
                    Spacer()
                    RecordButton(isRecording: isBusy, action: recordButtonTapped)
                    Spacer()
                    Button {
                        camera.flipCamera()
                    } label: {
                        Image(systemName: "arrow.triangle.2.circlepath.camera")
                            .font(.title)
                            .foregroundStyle(.white)
                            .frame(width: 64, height: 64)
                            .background(.black.opacity(0.4), in: Circle())
                    }
                    .disabled(isBusy)
                    .opacity(isBusy ? 0.3 : 1)
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
            }
            .padding(.top, 8)
        }
    }
}

/// Formats seconds as m:ss.
func formatDuration(_ seconds: Double) -> String {
    let total = Int(seconds.rounded())
    return String(format: "%d:%02d", total / 60, total % 60)
}

/// Top bar: a large "Choose audio" button that shows the picked file's name and length.
struct AudioChoiceBar: View {
    let chosen: ChosenAudio?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: "music.note")
                    .font(.title2)
                if let chosen {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(chosen.name).font(.headline).lineLimit(1)
                        Text(formatDuration(chosen.durationSeconds)).font(.subheadline).opacity(0.8)
                    }
                    Spacer()
                    Text("Change").font(.subheadline.bold())
                } else {
                    Text("Choose audio").font(.headline)
                    Spacer()
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, minHeight: 60)
            .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 16))
        }
        .padding(.horizontal, 16)
    }
}

/// A big round record button that turns into a rounded square while recording.
struct RecordButton: View {
    let isRecording: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle()
                    .stroke(.white, lineWidth: 5)
                    .frame(width: 84, height: 84)
                RoundedRectangle(cornerRadius: isRecording ? 8 : 35)
                    .fill(.red)
                    .frame(width: isRecording ? 34 : 70, height: isRecording ? 34 : 70)
            }
            .animation(.easeInOut(duration: 0.2), value: isRecording)
        }
        .accessibilityLabel(isRecording ? "Stop recording" : "Start recording")
    }
}

/// Shown when camera access is denied, with a shortcut to the app's Settings page.
struct PermissionDeniedView: View {
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "video.slash.fill")
                .font(.system(size: 56))
            Text("Camera access is off")
                .font(.title2.bold())
            Text("LipSync needs the camera to record your videos. Turn on Camera access for LipSync in Settings.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.white.opacity(0.8))
            Button {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    openURL(url)
                }
            } label: {
                Text("Open Settings")
                    .font(.headline)
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
        }
        .foregroundStyle(.white)
        .padding(32)
    }
}
