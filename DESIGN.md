# Omakase Plus design contract

This source candidate records the maintained interface contract. Technical, creative and deployment acceptance remain separate.

## Integrated player, November 1

The watch page follows the Geist/midnight/iris shell. A contain-fit 16:9 image
sits above one compact transport row: Play, explicit Next, time, volume,
fullscreen and gear. The reserved controls band is `52px + 3rem` (100px at the
normal root size), independent of the original ASS canvas coordinates. Fullscreen
uses the same band and container fullscreen. Controls retain 44px touch targets,
an opaque midnight backing, visible keyboard focus and phone-safe widths. The
actual video image, never a translated canvas, defines subtitle positioning.

The gear orders Quality, Audio, unified Subtitles and then Playback speed. Quality
lists actual video variants and labels a sole copy Original with its measured
resolution, or honestly says resolution unavailable. Audio remains Shaka's real
checked language/role selection. Subtitles combines native, original ASS and Off,
preserving exact-source restoration, font handling and exclusive renderer state.
Escape returns to the settings button. App PiP menus, context menu, shortcut,
document PiP and auto-PiP actions are disabled without changing browser settings.

The seek input is app-owned through public video events and requestAnimationFrame.
It paints the actual video clock, without interpolated watched time or a Shaka
private timer/configuration workaround. Drag previews commit an exact seek;
paused playback stays paused. Arrow/Page/Home/End range keys are isolated from
video shortcuts. Hidden, paused and reduced-motion playback use event updates
without continuous animation. The existing semantic watch events and five-second
progress sampler remain unchanged. `--stream-accent` supplies played range,
thumb, focus, selected settings and skip action; buffered/unplayed tracks remain
distinct on bright footage. Actual browser cadence and normal/fullscreen/phone
visual acceptance require separate measured receipts.

An optional trusted `skip_segments` envelope binds the exact media_version and
episode to at most four ordered, finite, non-overlapping intro/credits intervals
within the source duration. Both server and client validate it. The button appears
only inside an interval and seeks to its end, preserving post-credit content and
ordinary seek accounting. No trusted marker producer exists in the current
source contract; production responses are empty and no skip button is claimed
live. Synthetic intervals only qualify the bounded UI behavior.

Below the player: series link, episode heading and matching cached synopsis.
The desktop sidebar contains scoped navigation, season and cached genres, then
stacks on phones. Previous is shown only for a current published episode;
Next retains its exact session-qualified URL. No provider is fetched on page
open. Public scores and episode names are omitted when matching cached evidence
is unavailable. Restart, exact Plex fallback and subtitle license/source access
remain in a compact playback-options disclosure. Save feedback stays discreet;
actual failures remain an alert and stop buffered playback.

Play, preparation/cancel, deliberate bounded retry, session exit closure and
authorization retain their existing contracts. No next-episode autoplay. Reduced
motion disables decorative transitions; forced colors keeps visible boundaries.

## Viewing entry points

Continue Watching uses one playable cover per title with a visible Play or
Resume label and the exact episode number. The title underneath opens details.
A title without a qualified playback target opens details from its cover. The
episode list puts the playback control in a consistent trailing position and
keeps availability and watched state visible independently. Native links and
disclosures retain visible focus, with no action on focus alone.

For You allocates each anime to at most one shelf per screen. Narrow airing and
film shelves retain eligible candidates while personalized shelves rank by
viewing, ratings and genre. Already watched opens an optional score selector and
saves only on explicit submission.

The Settings landing page is a directory. Watching and account tasks come first.
Video quality uses familiar resolution names and a clear fallback order. More
detailed source rules are available under an Advanced download rules disclosure.
The form retains the same saved values and validation.

Movie watch history and rating save together with a clear receipt. A watched
movie can be reviewed through a read-only exact Plex identity check. The review
distinguishes a match, absence in the checked library and an inconclusive check.
Any copy-management action happens in Plex. The Omakase record remains saved.

## Apple TV client boundary

Keep catalog identity, exact episode numbering, scoped playback sessions,
duration, resume and completion semantics independent of browser presentation.
The web transport controls and ASS canvas are adapters for those contracts.
The native app can use the tvOS focus system for rows and the native playback
interface with qualified media. A future device check must cover authenticated
HLS, long seeks, resume, audio and subtitles, including a decision for styled
ASS, and Siri Remote focus. The web interface does not establish those results.

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

## Clear detail pages and request feedback

Detail artwork has a bounded, stable crop with a dark text fade. Expanding a
synopsis or verification disclosure grows the content without zooming the art.
Movie actions, availability, adjacent check details and personal rating controls
form one compact desktop reading area and stack deliberately on phones.

Movie request reports retain separate saved-record and fresh-observation dates.
Stale activity, unknown state, queued work, reported searching, no result, provider
restriction and connection failure have distinct text. Restrained status colors
support those words. A native request check exposes busy feedback, prevents
duplicates and preserves its outcome through the redirect. A request or check
never creates viewing history. Not watched is a display label; the editor keeps
an unset status distinct from a recorded choice.

Personal title links resolve exact owned identity and season. The home episode
action opens the next known episode; More info opens the overview. Shared titles
without a supported episode page expose one useful overview action. Discovery
continues to work for unowned or ambiguous titles.

Autocomplete dismissal clears visible status, popup, busy state and stale
responses together. Native scrolling keeps complete result text and posters
legible at the viewport edges. Slim dark/iris scrollbars have no arrow buttons;
forced colors retain system contrast and visible selection. Genre colors are
quiet metadata, and the footer uses aligned documentation, public repository and
Jhinx Dev links with roomy keyboard and touch targets.

## Movie discovery and library updates, November 1

Anime and movie For You share a poster-first hierarchy, iris metadata and clear
reasons beside each title. Movie filters live in a compact Customize disclosure.
Movie reasons describe the actual local genre ranking from saved ratings, or
public TMDB popularity/rating/release order; no movie AI fit score is invented.
Explore shares an eight-second deadline across genres, candidate metadata and
at most twenty detail lookups, with three concurrent details. Cached metadata
survives an upstream failure; available cards and honest partial/error feedback
return without starting acquisition, history import or another request.

Movie collection and discovery use the same freshness-aware status mapping as
details. Request failures, old reports, unknown activity, file readiness and
personal watched/rating records remain distinct. Public catalog candidates do
not imply ownership. A secondary History, ratings & import tools disclosure
explains local tracking, explicit Plex history import and protected manual values.
Opening the collection never imports history; last checked time remains visible
inside the tools.

Footer links use one continuous accent line across their complete text/icon box,
with an independent visible focus outline. Library updates retains native details
semantics, a visible open/closed chevron, a compact 44px summary hit area and
hover/focus state. Home groups identical visible activity results only, retains
each original attempt in event context and labels the separate View anime action.
Specific publication causes come from the exact run/monitor saved candidate code.
The historical generic SELECTION_DEFERRED record does not contain a specific
selection reason; the UI says so instead of inferring one from another receipt.
No source audit rows are changed or removed by grouping.

## Reversible personal list removal, November 1

Fullscreen entry uses a restrained 180ms opacity ease from .92 to 1 with
cubic-bezier(.2,.8,.2,1). Native requestFullscreen remains synchronous with the
trusted click; no size/position transform separates video, ASS or controls.
The existing reduced-motion rule disables this animation. Native entering,
exiting, rapid toggles and rendered frame timing remain browser acceptance gates.

Remove from Omakase list is a personal-library membership exclusion, with an
explicit Restore action on the retained title page. It works before airing and
with zero assets, without title typing. AniList membership/status, personal watch
history and ratings, Plex publication, provider files and other libraries remain
unchanged. Physical Plex/provider removal is a separate, labeled workflow; its
unusable confirmation form is omitted when no managed media exists.

Existing library_items removed rows hold the exclusion, without a migration or
enabling Households. Implicit personal anime binding/import cannot reactivate a
tombstone. Collection/Home omit it; due, explicit and in-flight monitor checks
exclude it unless another active library still references the same managed
resource. Original monitor scheduling flags remain intact, so Restore does not
guess an earlier active state. Only the selected personal membership's queued
local request events are cancelled; dispatched/provider work is not cancelled.
Household membership controls retain their separate existing scope and feature
gate; the new legacy-title action refuses a household library ID.

Saved file-check audio/subtitle languages are visible on the title. Missing or
undetermined evidence says Unknown; release titles never substitute for track
proof. Saved checks do not prove a changed source still has those tracks.
Rescan copy explicitly says it cannot create a Japanese audio track. Actual Plex
target links require their existing exact source metadata, never a guessed URL.


At360px and below, the compact transport omits only its mute button, retaining
Play, Next, readable wrapped time, fullscreen and gear as44px border-box targets.
Keyboard/hardware volume and mute remain available. This reserves time width
even when200% root text also doubles the page gutters. The owned seek input sits
above the supplied transparent controls for real pointer/touch hit testing and
yields below visible settings. Its click/double-click events stay local, so seeking
does not toggle surrounding playback/fullscreen. Rendered qualification is tied
to the subsequent exact-source browser receipt, not the earlier failed frames.


Mobile chrome keeps actual200% root text readable. Below700px the header uses a
wordmark column and a separate92px utilities column with fixed16px gutters. The
wordmark may wrap; bell/account retain44px targets. Redundant header Search is
omitted while the labeled dock Search stays available. Account/notification
panels anchor below the grown header rather than a fixed assumed header height.

The five dock destinations use icon-over-label columns with full wrapping labels,
not a horizontal icon/text pair. A44px +2.5rem dock reserve grows with root text,
with natural content height and safe-area padding. Body spacing and Browse's
popover offset use the same reserve, keeping content and the popover above the
dock. No nav label is clipped, ellipsized or scaled down to cancel text zoom.
This page-chrome qualification is separate from the unchanged player composite.


The initial Ready/preparation panel participates in page flow while visible.
Its real text, resume choice and Play/Cancel targets determine stage height,
including at200% root text. The video remains absolutely sized underneath during
setup, and initial timeline/transport layers stay hidden. Hiding the panel removes
these conditional rules and restores the already-qualified video/controls layout;
no renderer, watch sampling or playback JavaScript changes are involved.

## For You recommendations

For You uses artwork-first, horizontally scrollable shelves. Top Picks uses a
larger portrait rail. Continue Watching uses local viewing evidence. Genres,
liked-title matches, airing anime and films use bounded AniList candidates
ranked with local history. Movies and TV use the optional Seerr connection.
Source scores, library availability and personal viewing state use distinct
labels. One disclosure holds anime watchlist, library and feedback actions.

Render the page shell immediately. Standard shelves load independently of the
AI menu, use static skeletons during loading, and show a separate unavailable
state if their catalog cannot be reached. Ask for a menu opens the existing AI
workflow. Shelves keep native scrolling, keyboard controls and reduced-motion
behavior. Off-screen artwork loads lazily.
