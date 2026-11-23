import AVFoundation

/// A take combined with the song, ready to preview instantly or export.
/// Nothing is written to disk until `export` is called.
struct MergePlan {
    let composition: AVComposition
    let videoComposition: AVVideoComposition
}

enum VideoMergerError: LocalizedError {
    case noVideoTrack
    case noAudioTrack
    case exportFailed(String)

    var errorDescription: String? {
        switch self {
        case .noVideoTrack: "The recording has no video in it."
        case .noAudioTrack: "The audio file has no sound track in it."
        case .exportFailed(let reason): "Could not build the final video: \(reason)"
        }
    }
}

/// Combines the silent camera recording with the original audio file.
enum VideoMerger {
    /// - Parameters:
    ///   - audioURL: the song to lay over the video, or nil to keep it silent.
    ///   - audioDelay: seconds into the video at which the song starts. Positive
    ///     means the song starts later (fixes lips that lag the sound); negative
    ///     means the song is already playing when the video begins.
    ///   - frameAspect: width ÷ height of the on-screen camera preview. The
    ///     preview fills the screen by trimming the sides of the camera image,
    ///     so the video is trimmed the same way to come out exactly as it looked.
    static func build(videoURL: URL, audioURL: URL?, audioDelay: Double,
                      frameAspect: CGFloat?) async throws -> MergePlan {
        let composition = AVMutableComposition()

        // Video
        let videoAsset = AVURLAsset(url: videoURL)
        guard let sourceVideo = try await videoAsset.loadTracks(withMediaType: .video).first,
              let videoTrack = composition.addMutableTrack(withMediaType: .video,
                                                           preferredTrackID: kCMPersistentTrackID_Invalid)
        else { throw VideoMergerError.noVideoTrack }

        let videoRange = try await sourceVideo.load(.timeRange)
        let videoDuration = videoRange.duration
        try videoTrack.insertTimeRange(videoRange, of: sourceVideo, at: .zero)

        let transform = try await sourceVideo.load(.preferredTransform)
        let naturalSize = try await sourceVideo.load(.naturalSize)
        let frameRate = try await sourceVideo.load(.nominalFrameRate)
        // Tested on a phone: the camera writes frames that are already portrait
        // but still adds a 90° rotation note, so applying it turned the video
        // sideways. The app is portrait-only, so portrait frames are used as-is.
        let framesArePortrait = naturalSize.height > naturalSize.width
        let videoComposition = uprightVideoComposition(for: videoTrack,
                                                       transform: framesArePortrait ? .identity : transform,
                                                       naturalSize: naturalSize,
                                                       frameAspect: frameAspect,
                                                       frameRate: frameRate,
                                                       duration: videoDuration)

        // Audio: cut to the length of the video.
        if let audioURL {
            let audioAsset = AVURLAsset(url: audioURL)
            guard let sourceAudio = try await audioAsset.loadTracks(withMediaType: .audio).first,
                  let audioTrack = composition.addMutableTrack(withMediaType: .audio,
                                                               preferredTrackID: kCMPersistentTrackID_Invalid)
            else { throw VideoMergerError.noAudioTrack }

            let songRange = try await sourceAudio.load(.timeRange)
            let shift = CMTime(seconds: abs(audioDelay), preferredTimescale: 600)
            // Positive delay: silence first, song from its beginning.
            // Negative delay: video starts partway into the song.
            let insertAt = audioDelay >= 0 ? shift : .zero
            let songStart = audioDelay >= 0 ? songRange.start : songRange.start + shift
            let songLeft = songRange.end - songStart
            let length = CMTimeMinimum(videoDuration - insertAt, songLeft)
            if length > .zero {
                try audioTrack.insertTimeRange(CMTimeRange(start: songStart, duration: length),
                                               of: sourceAudio, at: insertAt)
            } else {
                composition.removeTrack(audioTrack)
            }
        }

        return MergePlan(composition: composition, videoComposition: videoComposition)
    }

    /// Turns the frames upright (exports don't reliably keep a track's rotation
    /// note) and trims them to the preview's shape, centred, the same way the
    /// preview's aspect-fill does.
    private static func uprightVideoComposition(for track: AVCompositionTrack,
                                                transform: CGAffineTransform,
                                                naturalSize: CGSize,
                                                frameAspect: CGFloat?,
                                                frameRate: Float,
                                                duration: CMTime) -> AVVideoComposition {
        // Where the frame lands after the transform; shift it back to the origin.
        let bounds = CGRect(origin: .zero, size: naturalSize).applying(transform)
        let uprightSize = CGSize(width: abs(bounds.width), height: abs(bounds.height))
        let renderSize = croppedSize(of: uprightSize, toAspect: frameAspect)
        // Centre the crop: move the frame left/up by half of what is trimmed.
        let cropX = (uprightSize.width - renderSize.width) / 2
        let cropY = (uprightSize.height - renderSize.height) / 2
        let upright = transform.concatenating(
            CGAffineTransform(translationX: -bounds.minX - cropX, y: -bounds.minY - cropY))

        let layer = AVMutableVideoCompositionLayerInstruction(assetTrack: track)
        layer.setTransform(upright, at: .zero)

        let instruction = AVMutableVideoCompositionInstruction()
        instruction.timeRange = CMTimeRange(start: .zero, duration: duration)
        instruction.layerInstructions = [layer]

        let videoComposition = AVMutableVideoComposition()
        videoComposition.renderSize = renderSize
        // Match the camera's frame rate rather than assuming one.
        let fps = frameRate > 0 ? CMTimeScale(frameRate.rounded()) : 30
        videoComposition.frameDuration = CMTime(value: 1, timescale: fps)
        videoComposition.instructions = [instruction]
        return videoComposition
    }

    /// The largest size with the given aspect that fits inside `size`, rounded
    /// to even numbers because video encoders require them.
    private static func croppedSize(of size: CGSize, toAspect aspect: CGFloat?) -> CGSize {
        func even(_ value: CGFloat) -> CGFloat { (value / 2).rounded(.down) * 2 }
        guard let aspect, aspect > 0 else { return CGSize(width: even(size.width), height: even(size.height)) }
        if size.width / size.height > aspect {
            return CGSize(width: even(size.height * aspect), height: even(size.height)) // trim the sides
        } else {
            return CGSize(width: even(size.width), height: even(size.width / aspect)) // trim top and bottom
        }
    }

    /// Writes the plan to a new .mp4 in the temp folder and returns its location.
    static func export(_ plan: MergePlan) async throws -> URL {
        guard let session = AVAssetExportSession(asset: plan.composition,
                                                 presetName: AVAssetExportPresetHighestQuality)
        else { throw VideoMergerError.exportFailed("this phone cannot export the video.") }
        session.videoComposition = plan.videoComposition
        // Index ("moov") at the front of the file. Meta refuses a video_url whose index is at the end,
        // and players can start streaming sooner.
        session.shouldOptimizeForNetworkUse = true

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("lipsync-\(UUID().uuidString).mp4")
        if #available(iOS 18, *) {
            try await session.export(to: url, as: .mp4)
        } else {
            session.outputURL = url
            session.outputFileType = .mp4
            await session.export()
            guard session.status == .completed else {
                throw VideoMergerError.exportFailed(session.error?.localizedDescription ?? "unknown error")
            }
        }
        return url
    }
}

/// Where the song was playing while recording. Bluetooth headphones add a
/// lot of delay before the sound is heard, so each output gets its own offset.
enum AudioOutput {
    case phoneSpeaker
    case bluetooth

    /// Seconds to delay the song in the final video so it lines up with the
    /// lips. Tuned by eye on Nash's iPhone and AirPods (2026-11-05).
    /// Wired headphones use the speaker value.
    var syncDelay: Double {
        switch self {
        case .phoneSpeaker: 0.33
        case .bluetooth: 0.53
        }
    }

    /// Reads the current route from the audio session.
    static var current: AudioOutput {
        let bluetooth: Set<AVAudioSession.Port> = [.bluetoothA2DP, .bluetoothLE, .bluetoothHFP]
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        return outputs.contains { bluetooth.contains($0.portType) } ? .bluetooth : .phoneSpeaker
    }
}
