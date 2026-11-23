import SwiftUI
import UIKit

/// Worker address and token, plus the links of recent uploads.
struct UploadSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var uploader = R2Uploader.shared

    @State private var workerURL = ""
    @State private var token = ""
    @State private var errorMessage: String?
    @State private var copiedKey: String?

    private var draft: UploadSettings { UploadSettings(workerURL: workerURL, token: token) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://lipsync-upload.….workers.dev", text: $workerURL)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Upload token", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Upload Worker")
                } footer: {
                    if !workerURL.isEmpty && draft.endpoint == nil {
                        Text("The address must start with https://").foregroundStyle(.red)
                    } else if UploadSettings.builtIn() != nil {
                        Text("Built into the app, so there's nothing to fill in. Anything you save here is kept in this phone's Keychain and used instead.")
                    } else {
                        Text("Stored in this phone's Keychain. The token is the Worker's UPLOAD_TOKEN.")
                    }
                }

                if UploadSettings.load() != nil && UploadSettings.builtIn() != nil {
                    Section {
                        Button("Use the built-in settings", role: .destructive) {
                            UploadSettings.clearSaved()
                            dismiss()
                        }
                    }
                }

                Section("Recent uploads") {
                    if uploader.recent.isEmpty {
                        Text("Nothing uploaded yet.").foregroundStyle(.secondary)
                    }
                    ForEach(uploader.recent) { record in
                        recentRow(record)
                    }
                    .onDelete { offsets in
                        offsets.map { uploader.recent[$0] }.forEach(uploader.removeRecord)
                    }
                }
            }
            .navigationTitle("Uploads")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save).disabled(!draft.isComplete)
                }
            }
            .onAppear {
                if let saved = UploadSettings.current() {
                    workerURL = saved.workerURL
                    token = saved.token
                }
            }
            .alert("Couldn't save", isPresented: Binding(get: { errorMessage != nil },
                                                         set: { if !$0 { errorMessage = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(errorMessage ?? "")
            }
        }
    }

    private func recentRow(_ record: UploadRecord) -> some View {
        Button {
            UIPasteboard.general.string = record.publicURL.absoluteString
            copiedKey = record.key
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(record.date, format: .dateTime.month().day().hour().minute())
                    Text(ByteCountFormatter.string(fromByteCount: record.bytes, countStyle: .file))
                        .foregroundStyle(.secondary)
                    if !record.verified {
                        Label("Unchecked", systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    Spacer()
                    Image(systemName: copiedKey == record.key ? "checkmark" : "doc.on.doc")
                }
                .font(.subheadline)
                Text(record.publicURL.absoluteString)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .tint(.primary)
    }

    private func save() {
        var cleaned = draft
        cleaned.workerURL = cleaned.workerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        cleaned.token = cleaned.trimmedToken
        do {
            try cleaned.save()
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
