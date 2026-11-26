import QualificationCore
import SwiftUI

/// Reorder, hide and restore Home shelves. The layout is stored per profile on the
/// server, so the same order shows on the web and on Apple TV.
struct PhoneHomeEditor: View {
    struct EditableRow: Identifiable, Equatable {
        let id: String
        let label: String
        var hidden: Bool
    }

    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    let onSaved: () async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var rows: [EditableRow]
    @State private var saving = false
    @State private var errorText: String?

    init(
        store: PhoneSessionStore,
        client: LiveLibraryClient,
        rows: [PhoneHomeProjection.Layout.Row],
        onSaved: @escaping () async -> Void
    ) {
        self.store = store
        self.client = client
        self.onSaved = onSaved
        _rows = State(initialValue: rows.map { EditableRow(id: $0.id, label: $0.label, hidden: $0.hidden) })
    }

    private var visibleCount: Int { rows.filter { !$0.hidden }.count }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach($rows) { $row in
                        HStack(spacing: 12) {
                            Text(row.label)
                                .foregroundStyle(row.hidden ? Color.appMuted : Color.appText)
                                .strikethrough(row.hidden, color: Color.appMuted)
                            Spacer()
                            Button {
                                row.hidden.toggle()
                            } label: {
                                Image(systemName: row.hidden ? "eye.slash" : "eye")
                                    .foregroundStyle(row.hidden ? Color.appMuted : Color.appAccent)
                                    .frame(minWidth: 44, minHeight: 44)
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel(row.hidden ? "Show \(row.label)" : "Hide \(row.label)")
                            .accessibilityIdentifier("phone.home.editor.toggle.\(row.id)")
                        }
                        .listRowBackground(Color.appSurface)
                    }
                    .onMove { rows.move(fromOffsets: $0, toOffset: $1) }
                } footer: {
                    Text(footerText)
                        .foregroundStyle(errorText == nil ? Color.appMuted : Color.appWarning)
                }
                Section {
                    Button("Reset to default layout", role: .destructive) {
                        Task { await reset() }
                    }
                    .disabled(saving)
                }
                .listRowBackground(Color.appSurface)
            }
            .environment(\.editMode, .constant(.active))
            .scrollContentBackground(.hidden)
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .scrollIndicators(.hidden)
            .navigationTitle("Edit Home")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        Task { await save() }
                    } label: {
                        if saving { ProgressView() } else { Text("Done").bold() }
                    }
                    .disabled(saving || visibleCount == 0)
                    .accessibilityIdentifier("phone.home.editor.done")
                }
            }
        }
        .interactiveDismissDisabled(saving)
    }

    private var footerText: String {
        if let errorText { return errorText }
        if visibleCount == 0 { return "Keep at least one shelf visible." }
        return "Drag to reorder. Tap the eye to hide or show a shelf. Changes sync to the web and Apple TV."
    }

    private func save() async {
        saving = true
        errorText = nil
        defer { saving = false }
        do {
            try await client.saveHomeLayout(rows: rows.map { ($0.id, $0.hidden) })
            await onSaved()
            dismiss()
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
            dismiss()
        } catch {
            errorText = "Couldn't save your Home layout. Check your connection and try again."
        }
    }

    private func reset() async {
        saving = true
        errorText = nil
        defer { saving = false }
        do {
            try await client.editHome(action: "reset")
            await onSaved()
            dismiss()
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
            dismiss()
        } catch {
            errorText = "Couldn't reset Home. Check your connection and try again."
        }
    }
}
