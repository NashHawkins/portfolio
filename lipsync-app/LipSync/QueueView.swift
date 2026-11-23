import AVFoundation
import AVKit
import SwiftUI

/// Uploads waiting in R2 for the Trial Reels batch, which posts each day's videos at 08:00 the next
/// morning (Denver time). Watch them, and delete any you don't want posted.
struct QueueView: View {
    @Environment(\.dismiss) private var dismiss

    @State private var day: QueueDay = .today
    @State private var listing: DayListing?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var playing: QueuedVideo?
    @State private var pendingDelete: QueuedVideo?
    @State private var deletingKeys: Set<String> = []

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Picker("Day", selection: $day) {
                        ForEach(QueueDay.allCases) { Text($0.title).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
                } footer: {
                    Text(Self.schedule(for: day)).font(.footnote)
                }

                Section(header: Text(header)) {
                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.red)
                    } else if let listing, listing.videos.isEmpty {
                        Text("No videos uploaded \(day == .today ? "today" : "yesterday").")
                            .foregroundStyle(.secondary)
                    } else if listing == nil {
                        HStack { Spacer(); ProgressView(); Spacer() }
                    }
                    ForEach(Array((listing?.videos ?? []).enumerated()), id: \.element.id) { index, video in
                        row(video, number: index + 1)
                            .swipeActions {
                                Button(role: .destructive) { pendingDelete = video } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                    }
                }
            }
            .navigationTitle("Queued videos")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task(id: day) { await load() }
            .refreshable { await load() }
            .confirmationDialog("Delete this video?", isPresented: Binding(get: { pendingDelete != nil },
                                                                           set: { if !$0 { pendingDelete = nil } }),
                                titleVisibility: .visible, presenting: pendingDelete) { video in
                Button("Delete from R2", role: .destructive) { Task { await delete(video) } }
            } message: { _ in
                Text("It is removed from the bucket and won't be posted. This can't be undone.")
            }
            .fullScreenCover(item: $playing) { video in
                QueuedPlayerView(video: video) {
                    playing = nil
                    Task { await delete(video) }
                }
            }
        }
    }

    private var header: String {
        guard let listing else { return day.title }
        let count = listing.videos.count
        return "\(listing.date) · \(count) video\(count == 1 ? "" : "s")"
    }

    private func row(_ video: QueuedVideo, number: Int) -> some View {
        Button { playing = video } label: {
            HStack(spacing: 12) {
                Thumbnail(url: video.publicURL)
                VStack(alignment: .leading, spacing: 4) {
                    Text("#\(number) · uploaded \(video.uploaded.formatted(date: .omitted, time: .shortened))")
                        .font(.headline)
                    Text(ByteCountFormatter.string(fromByteCount: video.size, countStyle: .file))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if deletingKeys.contains(video.key) {
                    ProgressView()
                } else {
                    Image(systemName: "play.circle.fill").font(.title2)
                }
            }
        }
        .tint(.primary)
        .disabled(deletingKeys.contains(video.key))
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let result = try await QueueClient.list(day)
            listing = result
            errorMessage = nil
        } catch is CancellationError {
        } catch {
            if (error as? URLError)?.code == .cancelled { return } // switched tabs mid-load
            listing = nil
            errorMessage = error.localizedDescription
        }
    }

    private func delete(_ video: QueuedVideo) async {
        pendingDelete = nil
        deletingKeys.insert(video.key)
        defer { deletingKeys.remove(video.key) }
        do {
            try await QueueClient.delete(video)
        } catch {
            errorMessage = error.localizedDescription
        }
        await load()
    }

    /// When the selected day's videos get posted, so a delete is never mistaken for an "unpost".
    static func schedule(for day: QueueDay, now: Date = Date()) -> String {
        guard day == .yesterday else { return "These are posted to Instagram as Trial Reels at 8:00 tomorrow morning." }
        var denver = Calendar(identifier: .gregorian)
        denver.timeZone = TimeZone(identifier: "America/Denver") ?? .current
        if denver.component(.hour, from: now) < 8 {
            return "These are posted at 8:00 this morning. Delete any you don't want before then."
        }
        return "The 8:00 run has already posted these. Deleting one now won't remove it from Instagram."
    }
}

/// A small first-frame preview, read straight from the public link.
private struct Thumbnail: View {
    let url: URL
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8).fill(.gray.opacity(0.25))
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            }
        }
        .frame(width: 48, height: 80)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .task(id: url) {
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 160, height: 280)
            if let frame = try? await generator.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)).image {
                image = UIImage(cgImage: frame)
            }
        }
    }
}

/// Plays one queued video on a loop, with a Delete button.
private struct QueuedPlayerView: View {
    let video: QueuedVideo
    let onDelete: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var player = AVPlayer()
    @State private var confirming = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VideoPlayer(player: player).ignoresSafeArea()
            VStack {
                HStack {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark")
                            .font(.title3.bold())
                            .padding(12)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .tint(.white)
                    .accessibilityLabel("Close")
                    Spacer()
                }
                .padding(.horizontal, 24)
                Spacer()
                Button(role: .destructive) { confirming = true } label: {
                    Label("Delete — don't post this", systemImage: "trash")
                        .font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
            }
        }
        .onAppear {
            player.replaceCurrentItem(with: AVPlayerItem(url: video.publicURL))
            player.actionAtItemEnd = .none
            player.play()
        }
        .onDisappear { player.pause() }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemDidPlayToEndTime)) { note in
            guard (note.object as? AVPlayerItem) === player.currentItem else { return }
            player.seek(to: .zero)
            player.play()
        }
        .confirmationDialog("Delete this video?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Delete from R2", role: .destructive, action: onDelete)
        } message: {
            Text("It is removed from the bucket and won't be posted. This can't be undone.")
        }
    }
}
