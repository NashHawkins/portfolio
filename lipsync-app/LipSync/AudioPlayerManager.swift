import AVFoundation

/// The audio clip the user picked, copied into the app's own storage.
struct ChosenAudio {
    let url: URL
    let name: String
    let durationSeconds: Double
}

/// Imports the chosen audio file and plays it through the speaker while recording.
///
/// @unchecked Sendable: @Published properties are only set on the main thread.
/// `play()` may be called from the capture delegate's thread (see CameraManager);
/// that is safe because `player` cannot change while a recording is in progress.
final class AudioPlayerManager: NSObject, ObservableObject, @unchecked Sendable {
    @Published private(set) var chosen: ChosenAudio?
    @Published private(set) var isPlaying = false
    @Published var errorMessage: String?

    /// Called when the clip plays to the end, so recording can stop with it.
    var onFinishedPlaying: (() -> Void)?

    /// Where the song was playing on the last take (speaker or Bluetooth),
    /// which decides how much the merge delays the song.
    private(set) var lastOutput: AudioOutput = .phoneSpeaker

    private var player: AVAudioPlayer?

    override init() {
        super.init()
        configureAudioSession()
    }

    /// `.playback` sends audio to the loud speaker (not the earpiece) and keeps
    /// playing even when the ring/silent switch is set to silent.
    private func configureAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default)
            try session.setActive(true)
        } catch {
            errorMessage = "Could not set up audio playback: \(error.localizedDescription)"
        }
    }

    // MARK: - Importing

    /// Copies the file the user picked into Documents/ChosenAudio. Files from the
    /// Files app are only readable while security-scoped access is on, so we make
    /// our own copy to keep it readable later (for playback and for the merge).
    func importAudio(from pickedURL: URL) {
        let didAccess = pickedURL.startAccessingSecurityScopedResource()
        defer { if didAccess { pickedURL.stopAccessingSecurityScopedResource() } }

        player = nil
        chosen = nil

        do {
            let fileManager = FileManager.default
            let folder = URL.documentsDirectory.appendingPathComponent("ChosenAudio", isDirectory: true)
            try? fileManager.removeItem(at: folder) // Keep only the latest pick.
            try fileManager.createDirectory(at: folder, withIntermediateDirectories: true)

            let destination = folder.appendingPathComponent(pickedURL.lastPathComponent)
            try fileManager.copyItem(at: pickedURL, to: destination)

            let newPlayer = try AVAudioPlayer(contentsOf: destination)
            newPlayer.delegate = self
            newPlayer.prepareToPlay()
            player = newPlayer
            chosen = ChosenAudio(url: destination,
                                 name: pickedURL.lastPathComponent,
                                 durationSeconds: newPlayer.duration)
        } catch {
            errorMessage = "Could not open that audio file: \(error.localizedDescription)"
        }
    }

    // MARK: - Playback

    /// Rewinds and preloads the audio buffers so `play()` starts with minimal delay.
    func prepare() {
        configureAudioSession() // Another app (e.g. Music) may have taken the session.
        player?.currentTime = 0
        player?.prepareToPlay()
    }

    /// Called directly from the recording-started callback. It deliberately does
    /// not hop to the main thread first, because every millisecond of delay here
    /// shows up as an audio/video offset.
    func play() {
        guard let player else { return }
        lastOutput = .current
        player.play()
        DispatchQueue.main.async { self.isPlaying = true }
    }

    func stop() {
        player?.stop()
        player?.currentTime = 0
        DispatchQueue.main.async { self.isPlaying = false }
    }
}

// MARK: - AVAudioPlayerDelegate

extension AudioPlayerManager: AVAudioPlayerDelegate {
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async {
            self.isPlaying = false
            self.onFinishedPlaying?()
        }
    }
}
