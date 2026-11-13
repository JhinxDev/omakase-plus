# Shared libraries, personal records

**Released optional capability.** `OMAKASE_PLUS_HOUSEHOLDS_ENABLED` defaults off
on new installations and requires private mode. Each installation needs operator
qualification of its app/worker/curator release and combined migrations before
activation. Existing users are not silently moved into a shared library. App-only
use remains available without a household.

## A household is a library, not a shared login

| Shared | Personal |
| --- | --- |
| Catalog membership, compatible media copies and owner-controlled connectors | Progress, ratings, tracker credentials, language preferences and selected playback tracks |

For example, two members can use one multi-audio movie while keeping separate
ratings and choosing different audio tracks. A shared item is not evidence that
every member watched it or can play it through the same external account.

## Start deliberately

1. An owner opens **Settings → Household** and chooses an empty shared library
   or reviews the existing personal-library promotion preview.
2. The owner sends a time-limited invitation. The recipient signs in to their own
   Omakase account, reviews the target library and explicitly accepts.
3. Each person chooses Personal or Shared from the library selector. Changing
   scope changes visible library items, not their identity or personal history.
4. In **Settings → Household → Members**, the owner can let each person request
   titles for review, add titles to the catalog, or use viewing access alone.
   An account with no custom setting inherits the household's add-title default.
   The owner configures library sources separately. Membership does not expose
   connector keys.

A member can choose **Find a title** in Household settings, search the catalog,
and select **Request title**. Movie and TV discovery use the catalog connection
shared by the owner. Anime requests use the reviewed AniList title and season.
The owner's notification names the requester and links to the pending decision.
Approval adds the exact title to the shared catalog. Declining leaves the catalog
alone. The requester sees the decision in Household settings. Removing a member
or turning off their title requests cancels their pending suggestions.

Requesting a title, adding it to the catalog and acquiring media are separate
actions. A media acquisition request records
its actual member actor, owner acquisition criteria and allowed effective language
rule. It does not borrow the owner's tracker identity. New operational monitors
remain inactive until the qualified shared dispatcher can use that frozen request.
Queued/unavailable means no supported dispatch occurred, not a successful download.

## Keep, remove and revoke

Members can withdraw their own keep request. Owner catalog-only removal does not
delete files or personal history. Physical deletion stays blocked by active shared
bindings or unresolved keep/use/watching claims and retains the separate removal
review/confirmation flow. Never remove another person's working media to satisfy
new language preferences.

Revoking an invitation prevents its future use. Removing a member rechecks future
library access and request dispatch; it does not erase the member's personal
ratings/history or grant the owner access to personal tracker tokens. A Plex
account binding must be independently verified; library membership alone does not
prove which viewer selected a stream or watched a title.

## Operator readiness

The owner must test source access explicitly. Language publication qualification
is a separate capability: unknown physical tracks stay unknown, and unavailable
adapters remain queued with no provider request. Movie requests do not gain Riven
language enforcement merely because a language preference was recorded. TV
acquisition remains unsupported. Review the exact release's supported adapters
before enabling shared intake.

Back up the DB and encryption key and qualify combined migrations 033–036 upgrade,
protected personal records and a qualified current-data recovery route before
activation. Never infer unrestricted old-version write compatibility from an
additive migration. See [current-data recovery](current-data-recovery.md).
No installation prompt should expose the app publicly or enable acquisition merely
to demonstrate household setup. See [language settings](language-settings.md),
[personal tracker sync](myanimelist-bridge.md) and the
[operator guide](https://omakase-plus.jhinx.dev/docs/backup-upgrade-recovery).
