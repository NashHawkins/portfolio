import AVKit
import Photos
import SwiftUI
import UIKit

/// The take you just recorded, waiting for a keep-or-retake decision.
struct ReviewItem: Identifiable {
    let id = UUID()
    /// The silent camera file.
    let videoURL: URL
    /// The song, or nil if none was picked.
    let audioURL: URL?
    /// Where the song was playing while recording; sets the sync offset.
    let output: AudioOutput
    /// Width ÷ height of the camera preview, so the video matches what was on screen.
    let frameAspect: CGFloat?
}

/// Plays the take with the song on a loop. From here you can upload it to
/// R2, save it to the camera roll, or delete it.
///
/// The preview plays straight from the composition, so it opens instantly.
/// The file is exported once, the first time Save or Upload needs it.
struct ReviewView: View {
    let item: ReviewItem
    /// Called once the take is dealt with (saved or deleted). Its files are already gone.
    let onDone: () -> Void

    @State private var player = AVPlayer()
    @State private var plan: MergePlan?
    @State private var buildTask: Task<Void, Never>?
    @State private var isSaving = false
    @State private var errorMessage: String?
    /// The single export shared by Save and Upload.
    @State private var exportTask: Task<URL, Error>?
    @State private var isExportingForUpload = false
    @State private var showingUploadSettings = false
    @State private var copiedLink = false
    @ObservedObject private var uploader = R2Uploader.shared

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if plan != nil {
                VideoPlayer(player: player)
                    .ignoresSafeArea()
            } else {
                ProgressView("Preparing your video…").tint(.white).foregroundStyle(.white)
            }

            VStack(spacing: 12) {
                HStack {
                    Spacer()
                    Button { showingUploadSettings = true } label: {
                        Image(systemName: "gearshape.fill")
                            .font(.title3)
                            .padding(10)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .tint(.white)
                    .accessibilityLabel("Upload settings")
                }
                .padding(.horizontal, 24)
                Spacer()
                uploadControl
                actionButtons
            }
        }
        .onAppear {
            player.actionAtItemEnd = .none
            build()
        }
        .onDisappear {
            buildTask?.cancel()
            player.pause()
        }
        .sheet(isPresented: $showingUploadSettings) {
            UploadSettingsView()
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemDidPlayToEndTime)) { note in
            // Loop the preview.
            guard (note.object as? AVPlayerItem) === player.currentItem else { return }
            player.seek(to: .zero)
            player.play()
        }
        .alert("Something went wrong", isPresented: Binding(get: { errorMessage != nil },
                                                           set: { if !$0 { errorMessage = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "")
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 16) {
            Button(role: .destructive, action: discard) {
                Label("Delete & retake", systemImage: "trash")
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.bordered)
            .tint(.red)

            Button(action: save) {
                Group {
                    if isSaving {
                        ProgressView().tint(.white)
                    } else {
                        Label("Save", systemImage: "square.and.arrow.down")
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
            .disabled(plan == nil)
        }
        .font(.headline)
        .disabled(isSaving)
        .padding(.horizontal, 24)
        .padding(.bottom, 24)
    }

    // MARK: - Upload

    /// This take's upload state. Another take's finished upload doesn't show here.
    private enum UploadRow {
        case ready, otherTakeUploading, phase(UploadPhase)
    }

    private var uploadRow: UploadRow {
        if uploader.ownerID == item.id { return .phase(uploader.phase) }
        return uploader.phase.isBusy ? .otherTakeUploading : .ready
    }

    @ViewBuilder
    private var uploadControl: some View {
        VStack(spacing: 6) {
            switch uploadRow {
            case .ready, .phase(.idle):
                uploadButton("Upload to R2")
            case .otherTakeUploading:
                statusPill(Text("Another take is still uploading…"), progress: nil)
            case .phase(.preparing):
                statusPill(Text("Getting an upload link…"), progress: nil)
            case .phase(.uploading(let fraction)):
                statusPill(Text("Uploading \(Int(fraction * 100))%"), progress: fraction)
            case .phase(.verifying):
                statusPill(Text("Checking the public link…"), progress: nil)
            case .phase(.done(let record)):
                Button { copyLink(record) } label: {
                    Label(copiedLink ? "Link copied" : "Copy link",
                          systemImage: copiedLink ? "checkmark" : "link")
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                Text(record.publicURL.absoluteString)
                    .font(.caption.monospaced())
                    .foregroundStyle(.white.opacity(0.8))
                    .lineLimit(1)
                    .truncationMode(.middle)
            case .phase(.failed(let message)):
                uploadButton("Retry upload")
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.white)
                    .padding(8)
                    .background(.red.opacity(0.75), in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .font(.headline)
        .padding(.horizontal, 24)
    }

    private func uploadButton(_ title: String) -> some View {
        Button(action: upload) {
            Group {
                if isExportingForUpload {
                    ProgressView().tint(.white)
                } else {
                    Label(title, systemImage: "icloud.and.arrow.up")
                }
            }
            .frame(maxWidth: .infinity, minHeight: 56)
        }
        .buttonStyle(.bordered)
        .tint(.white)
        .disabled(plan == nil || isExportingForUpload || isSaving)
    }

    private func statusPill(_ text: Text, progress: Double?) -> some View {
        VStack(spacing: 8) {
            text.foregroundStyle(.white)
            if let progress {
                ProgressView(value: progress).tint(.white)
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 56)
        .padding(.horizontal, 16)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private func upload() {
        guard UploadSettings.current()?.isComplete == true else {
            showingUploadSettings = true
            return
        }
        isExportingForUpload = true
        copiedLink = false
        Task {
            defer { isExportingForUpload = false }
            do {
                let file = try await exportedFile()
                await uploader.upload(fileAt: file, owner: item.id)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func copyLink(_ record: UploadRecord) {
        UIPasteboard.general.string = record.publicURL.absoluteString
        copiedLink = true
    }

    // MARK: - Preview

    private var audioDelay: Double { item.audioURL == nil ? 0 : item.output.syncDelay }

    private func build() {
        let delay = audioDelay
        buildTask = Task {
            do {
                let newPlan = try await VideoMerger.build(videoURL: item.videoURL,
                                                          audioURL: item.audioURL,
                                                          audioDelay: delay,
                                                          frameAspect: item.frameAspect)
                guard !Task.isCancelled else { return }
                let playerItem = AVPlayerItem(asset: newPlan.composition)
                playerItem.videoComposition = newPlan.videoComposition
                player.replaceCurrentItem(with: playerItem)
                plan = newPlan
                player.play()
            } catch {
                guard !Task.isCancelled else { return }
                errorMessage = error.localizedDescription
            }
        }
    }

    // MARK: - Decisions

    /// Exports the take once; later calls get the same file.
    private func exportedFile() async throws -> URL {
        guard let plan else { throw VideoMergerError.exportFailed("the video isn't ready yet.") }
        if let exportTask { return try await exportTask.value }
        let task = Task { try await VideoMerger.export(plan) }
        exportTask = task
        do {
            return try await task.value
        } catch {
            exportTask = nil // let the next tap try again
            throw error
        }
    }

    /// Deletes this take's files. A running upload has its own copy and carries on.
    private func cleanUp() {
        try? FileManager.default.removeItem(at: item.videoURL)
        if let exportTask {
            Task { if let url = try? await exportTask.value { try? FileManager.default.removeItem(at: url) } }
        }
    }

    private func discard() {
        player.pause()
        cleanUp()
        onDone()
    }

    private func save() {
        guard plan != nil else { return }
        isSaving = true
        player.pause()
        Task {
            defer { isSaving = false }
            let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
            guard status == .authorized || status == .limited else {
                errorMessage = "LipSync isn't allowed to add to your photos. Turn on Photos access for LipSync in Settings."
                return
            }
            do {
                let finalURL = try await exportedFile()
                try await PHPhotoLibrary.shared().performChanges {
                    _ = PHAssetCreationRequest.creationRequestForAssetFromVideo(atFileURL: finalURL)
                }
                cleanUp() // Photos keeps its own copy.
                onDone()
            } catch {
                errorMessage = error.localizedDescription
                player.play()
            }
        }
    }
}
