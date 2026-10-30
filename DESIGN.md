# Omakase Plus design contract

This source candidate records the maintained interface contract. Technical, creative and deployment acceptance remain separate.

## Personal cinema

Artwork leads; one primary action follows. Geist is served locally. Pale iris carries selection/action, emerald confirmed availability, amber partial/attention, slate unknown, muted red destructive/failure. Every state has meaningful words as well as color. Availability, watching, rating and media verification are independent.

Home uses a 64–72vh cinematic stage, a genuine wide image, long text-protection gradient and bottom fade. Preserve the artwork's midtones. Inadequate landscape images fall back to a crisp source-sized portrait with its exact intrinsic aspect, one rounded visible edge and a restrained shadow. Sample eight narrow regions around the displayed image's perimeter. Blend each region at its corresponding visible corner or side, including the lower center; a small saturated patch must not dominate a different edge. Recalculate anchors after resizing, refresh colors on image loads, and clear them on unreadable artwork. The text keeps dark protection while the lower colored edge remains visible. There is no enlarged blurred duplicate or translucent letterbox matte. No automatic rotation. Phone art leads above a compact text block. Reduced motion is fully composed immediately.

## Shared components

Anime, Movies and TV share CollectionSearch: the same surface, keyline, corner, spacing and 48px controls. Scope is explicit; collection filtering differs from catalog discovery. Dense cards reserve three title lines with clipping and full accessible name, then use normal-flow metadata, progress, availability and quality rows. Never overlap text or call zero available episodes ready.

TV cards use observed season counts; only reconciled released sets establish completeness. Unknown coverage stays unknown. Removal is a separate confirmed library action naming actual destinations and retained records. Shared assets remain protected.

## Interaction and reach

Minimum touch targets 44px. Keyboard focus is visible; disclosures and menus support native keyboard behavior. Shelves use native momentum and snapping on touch, clickable arrows on desktop. Phone navigation respects safe-area insets. 320px and 200% text must remain readable without page overflow.

## Proof

Technical pass, rendered creative acceptance, production identity and physical-device acceptance are separate. Check true landscape, portrait, tiny and missing art; real long titles; stale/partial season evidence; removal states using isolated fixtures. Runtime design tokens are maintained in the CSS under src/omakase/plus/static.

## Notification center

The Account utility group includes a 44px notification bell. It uses one deferred, deadline-bound same-origin local-DB read per page; there is no polling or provider lookup. Unknown/loading/error never claims zero unread. The compact nonmodal panel uses the existing Geist/midnight/iris system, 14px corners, 14–16px body/title text, current saved rating, and neutral media-type art placeholders when no cached artwork is provided.

Primary links open the existing rating or guarded removal-review flow. Each completed title has one card. When both reminders apply, it offers separate Rate title and Review media actions, with one unread/dismiss state and a compact 44px Dismiss cross. Saving a rating keeps the media-review action; a genuinely new completion or managed asset can create a fresh reminder. Primary-link activation marks read through a nonblocking CSRF-protected keepalive POST; dismissal is a separate account-scoped action. Both use the returned feed without another GET. The panel supports Close, Escape, outside dismissal and focus return, and closes when keyboard focus moves outside. Phones use viewport-bounded height, an independently scrolling list and safe-area gutters. Readiness and saved progress remain distinct; a reminder performs no import, provider call, acquisition or removal.

### Notification artwork

Show the media poster in each notification instead of a media-type abbreviation when artwork is available. Keep the tile size stable, crop covers consistently, and load images only after the panel opens. Resolve artwork through existing local routes; a missing or failed image falls back without shifting the text or actions.

## 2026-10-28 settings, search and viewing-history refinement

Autocomplete hints and results follow the input edges, including narrow screens.
Mobile catalog controls stack to keep a full-width field; long suggestions use two
lines while retaining their complete accessible text and keyboard selection.

Saved credentials expose presence and connection health separately. Replacement
is explicit and cancellable; Remove key takes precedence over a typed replacement.
No old secret is returned to the browser.

Health warnings name the saved failure evidence and link to the relevant settings.
Host-managed metadata sources explain the actual operator configuration boundary.

Viewing history first uses an authenticated exact-episode still. When exact imagery is unavailable, a loaded series cover is visibly labeled Series artwork; if neither image is available, an episode-specific accessible placeholder remains. A series cover never claims to be an episode still. Series-level manual completion says Series completed, and a progress-only event says Progress updated. Movies and original history timestamps keep their established behavior.

The header shows the saved display name beside the avatar, with Account as the empty-name fallback. Long names truncate visually while the full escaped name remains accessible. Anime collection tabs use one active underline.

## 2026-10-29 task hierarchy and history identity

A single personal library needs no global switch form. Account retains its library
context and settings link. Genuine choices use a compact selected-library disclosure
with a native select and explicit submit; a shared selection remains visible by name.
Unavailable navigation offers a compact recovery link and never invents a scope.
Switching retains CSRF protection and does not occur on focus or selection alone.

Collection controls are one compact toolbar, with explicit saved/catalog scope,
one result count, 48px native controls and an accessible advanced-filter disclosure.
Movie history uses the actual film title as its heading. Episodic history keeps
readable normal-case show identity above the episode/event label. Core history
identity, source and date remain readable on phones; exact timestamps, watched
progress, personal ratings and artwork fallback labels retain their meanings.

## Continue watching and featured title

Order all eligible in-progress titles by this viewer's accepted viewing activity
before applying the ten-card shelf limit. Normalize timestamp offsets. Ignore
rating-only edits, operational updates, rejected/inferred observations and bulk
imports. A Plex season snapshot uses its validated season last-viewed timestamp
for title recency; import time is not new viewing. This must not turn an aggregate
season timestamp into an exact episode watch date in History. Unknown recency
follows dated titles with the existing stable library order as a tie fallback.

When Continue watching has items, the cinematic stage uses that same first item.
A partially available title retains its honest availability and details action;
an older fully ready title must not displace it. Use the ready-to-start fallback
only when no in-progress title exists. Current viewer and selected-library access
boundaries continue to govern the eligible cards.

## Settings navigation affordances

Account, Your experience and Your services are static group headings, visually
separated from destinations. Every desktop destination is a consistent bordered
row with a subtle surface and a decorative forward cue. Inactive links remain
clearly actionable; hover and keyboard focus are visible. The current destination
keeps a stronger iris surface and inset marker with exactly one aria-current.
Named semantic groups do not add headings to the tab order or change routes.

Phones keep the compact horizontal settings rail and hide group headings. Links
become clear bordered chips with44px targets; keyboard focus can scroll a chip
into view. Decorative arrows are hidden here to preserve space. Existing optional
Household visibility, identity, forms, permissions and settings behavior stay intact.
While a phone settings link has keyboard focus, proximity snapping yields to the
browser's focus scrolling. The overflowing rail also reveals a focused link with
native scrollIntoView using nearest alignment, keeping its target and outline
reachable without changing media-shelf controls.

## Text and separate actions

Direct explanatory paragraphs in editorial, utility and empty-state panels have
20px of separation before a following action group, standalone button or form.
This is a shared block boundary, including Household's create/join choices and
Storage's management link. Natural wrapping and existing 44/48px targets remain.
Inline controls and paragraphs elsewhere keep their existing compact rhythm.

## Episode rechecks and quality

The episode section owns one explicit season refresh action for missing episodes
and, on qualified installations,1080p anime replacements. An installation without
upgrade qualification keeps its missing-only action and explains that upgrades
are unavailable; an explicit upgrade request still fails closed. Anime prefers1080p and uses720p as fallback;2160p
is mainly for movies and TV. Preserve existing higher copies. Current copies
remain until a qualified publication selects a replacement. Downloading, a found
release and successful publication are distinct states, with actual pending work
and honest busy/no-result/blocked outcomes. Repeated submission is prevented and
back navigation restores the action.

Each episode card uses a real separate quality row. Amber identifies720p
fallbacks;1080p stays quieter. Release claims remain labeled release quality.
Measured dimensions require current source, exact file and episode proof. A new
higher release cannot hide the still-selected fallback. Phones stack the section
heading and action with breathing room; watch controls span the full title grid.

A previously completed run can return when its exact next aired episode exists.
Retain its original completion and rating until an explicit guarded continue and
mark-watched action. Metadata/GET rendering does not edit watch history. Counts
alone never claim that exact new episode is in Plex. The shelf and hero retain
actual viewing recency, with a View new episode details action when appropriate.
