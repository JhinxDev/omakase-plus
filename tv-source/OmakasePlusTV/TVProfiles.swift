import QualificationCore
import Security
import SwiftUI

@MainActor
struct TVProfiles: View {
    let roster: LiveProfileRoster
    let active: LiveSavedProfile.ID?
    let busy: Bool
    let error: String?
    let choose: (LiveSavedProfile) -> Void
    let add: () -> Void
    let done: () -> Void

    @Environment(\.resetFocus) private var resetFocus
    @Namespace private var pickerScope
    @FocusState private var focus: Item?
    private enum Item: Hashable {
        case profile(LiveSavedProfile.ID), add, done
    }

    private var preferredFocus: Item {
        (active ?? roster.entries.first?.id).map(Item.profile) ?? .add
    }

    private var columns: Int { max(1, min(roster.entries.count, 4)) }
    private var showsServer: Bool { Set(roster.entries.map(\.serverAddress)).count > 1 }

    var body: some View {
        VStack(spacing: 24) {
            Text("OMAKASE PLUS").font(.headline.weight(.semibold)).tracking(6)
                .foregroundStyle(Color.tvAccent)
            Text("Who’s watching?")
                .font(.system(size: 64, weight: .bold))
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("profiles.heading")
            Text("Choose a profile to pick up where you left off.")
                .font(.title3)
                .foregroundStyle(Color.tvMuted)
            LazyVGrid(columns: Array(repeating: GridItem(.fixed(232), spacing: 24), count: columns), spacing: 24) {
                ForEach(Array(roster.entries.enumerated()), id: \.element.id) { index, entry in
                    let current = entry.id == active
                    let name = entry.profile.name == "Profile" ? "Profile \(index + 1)" : entry.profile.name
                    Button { choose(entry) } label: {
                        profileTile(entry, name: name, current: current)
                    }
                    .buttonStyle(TVLibraryTileStyle(focused: focus == .profile(entry.id)))
                    .focusEffectDisabled()
                    .focused($focus, equals: .profile(entry.id))
                    .zIndex(focus == .profile(entry.id) ? 1 : 0)
                    .accessibilityLabel(name)
                    .accessibilityValue(showsServer ? entry.serverAddress : "")
                    .accessibilityAddTraits(current ? .isSelected : [])
                }
            }
            .frame(width: CGFloat(columns * 232 + (columns - 1) * 24))
            if let error {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(Color.tvWarning)
            }
            if busy { ProgressView("Opening profile…") }
            HStack(spacing: 20) {
                Button(action: add) { Label("Add profile", systemImage: "plus") }
                    .buttonStyle(TVActionStyle(focused: focus == .add))
                    .focusEffectDisabled()
                    .focused($focus, equals: .add)
                    .disabled(roster.entries.count >= LiveProfileRoster.capacity)
                if active != nil {
                    Button("Done", action: done)
                        .buttonStyle(TVActionStyle(focused: focus == .done))
                        .focusEffectDisabled()
                        .focused($focus, equals: .done)
                }
            }
        }
        .padding(.horizontal, 84)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.tvBackground.ignoresSafeArea())
        .disabled(busy)
        .focusScope(pickerScope)
        .defaultFocus($focus, preferredFocus)
        .task(id: busy) {
            guard !busy else { return }
            await Task.yield()
            guard !Task.isCancelled else { return }
            resetFocus(in: pickerScope)
            focus = preferredFocus
        }
        .onExitCommand { if !busy, active != nil { done() } }
    }

    private func profileTile(_ entry: LiveSavedProfile, name: String, current: Bool) -> some View {
        let focused = focus == .profile(entry.id)
        return VStack(spacing: 10) {
            TVProfileAvatar(entry: entry)
            Text(name)
                .font(.system(size: 36, weight: .semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .foregroundStyle(Color.tvText)
            if showsServer {
                Text(entry.serverAddress).font(.caption2).lineLimit(1).foregroundStyle(Color.tvMuted)
            }
            ZStack {
                if current {
                    Label("Current profile", systemImage: "checkmark.circle.fill")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.tvAccent)
                }
            }
            .frame(height: 28)
            .accessibilityHidden(true)
        }
        .padding(.horizontal, 14)
        .frame(width: 232, height: showsServer ? 246 : 218)
        .background(focused ? Color.tvRaised : Color.tvSurface, in: .rect(cornerRadius: 16))
        .overlay {
            RoundedRectangle(cornerRadius: 16)
                .strokeBorder(focused ? Color.tvFocus : Color.tvKeyline, lineWidth: focused ? 3 : 1)
        }
    }
}

private struct TVProfileAvatar: View {
    let entry: LiveSavedProfile
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Color.tvRaised
            if entry.profile.hasAvatar, let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: "person.fill")
                    .font(.system(size: 39, weight: .medium))
                    .foregroundStyle(Color.tvAccent)
            }
        }
        .frame(width: 86, height: 86)
        .clipShape(Circle())
        .overlay { Circle().strokeBorder(Color.tvKeyline, lineWidth: 2) }
        .accessibilityHidden(true)
        .task(id: entry.expiresAt) {
            guard entry.profile.hasAvatar, let client = try? LiveLibraryClient.restore(entry.credential),
                  let data = try? await client.profileAvatar(), !Task.isCancelled else { return }
            image = UIImage(data: data)
        }
    }
}

enum TVLibraryKeychain {
    enum Storage {
        case roster(LiveProfileRoster)
        case legacy(LiveLibraryClient.Credential)
    }

    struct Unavailable: Error {}

    private static let lookup: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "dev.omakase.plus.tv.library",
        kSecAttrAccount as String: "active",
    ]

    static func read() throws -> Storage {
        var query = lookup
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return .roster(LiveProfileRoster()) }
        guard status == errSecSuccess, let data = result as? Data else { throw Unavailable() }
        let decoder = JSONDecoder()
        if let roster = try? decoder.decode(LiveProfileRoster.self, from: data) {
            return .roster(roster)
        }
        return .legacy(try decoder.decode(LiveLibraryClient.Credential.self, from: data))
    }

    static func save(_ roster: LiveProfileRoster) throws {
        let data = try JSONEncoder().encode(roster)
        let value = [kSecValueData as String: data]
        var status = SecItemUpdate(lookup as CFDictionary, value as CFDictionary)
        if status == errSecItemNotFound {
            var item = lookup
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Unavailable() }
    }
}
