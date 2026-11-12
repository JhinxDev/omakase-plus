# Omakase Plus design contract

This source candidate records the maintained interface contract. Technical, creative and deployment acceptance remain separate.

## Integrated player, November 1

The watch page follows the Geist/midnight/iris shell. The video preserves its
aspect ratio. Desktop playback fills the browser viewport with the title and
navigation above the picture and one compact transport row below. The overlays
hide after three seconds of pointer inactivity while playing and return on
pointer or keyboard input. A focused control or an open menu keeps them visible.
The compact phone layout reserves `52px + 3rem` for the transport row. Browser
fullscreen remains available in either layout. Playback failures show recovery
actions inside the desktop viewport.
The desktop volume slider stays at its full width. Controls retain 44px touch
targets, a midnight backing, visible keyboard focus and phone-safe widths.
The video image defines subtitle positioning.

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
without continuous animation. A queued seek keeps the newest requested position
visible while its stream prepares. Playback evidence uses the last stable
position while the transport moves, then anchors at the confirmed destination.
Startup waits for the saved position in a growing stream before playing or
recording progress.
Transport tooltips sit above the timeline in the picture area. The existing
semantic watch events and five-second progress sampler still record playback.
`--stream-accent` supplies played range,
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
stacks on phones. Previous and Next appear when the exact adjacent episode is
present in the current scoped library. Starting an episode still rechecks access
to its copy. The page renders its context from local data. Public scores and episode names are omitted when matching cached evidence
is unavailable. Restart, exact Plex fallback and subtitle license/source access
remain in a compact playback-options disclosure. Save feedback stays discreet;
actual failures remain an alert and stop buffered playback.
On the watch page, the focused skip link sits in its own row above the player
title. Dark `#171325` text on iris `#c3b1ff` keeps the label legible. A playback
failure removes transport and timeline controls and moves focus from the
player to recovery. Fullscreen exits so the recovery action remains visible.
The recovery panel includes a filled iris retry or reload button and the
Back to title link in the same keyboard order. On phones it replaces the
empty video frame so the actions stay close to the title.
An unavailable next episode uses a dimmed icon and accessible label.

Play, Resume, and episode navigation open the same stable player frame. The
loading indicator stays in the video area through startup and seeks. The player
offers Play if the browser requires a gesture. The next episode starts after
the viewer chooses it. A compact focus cue follows the timeline thumb for
keyboard users. Reduced motion disables the spinner animation and forced colors
keeps its boundaries visible.

## Viewing entry points

Continue Watching uses one playable cover per title with a visible Play or
Resume label and the exact episode number. The title underneath opens details.
A title without a qualified playback target opens details from its cover. The
episode list puts the playback control in a consistent trailing position and
keeps availability and watched state visible independently. Native links and
disclosures retain visible focus, with no action on focus alone.

Anime, Movies and TV Shows share Collection, Watching, Watched and Available.
Available describes confirmed library presence or a qualified in-app episode
source. A Play or Resume action still needs its own playback evidence. Provider
and request details live in secondary controls. Shelves and collections align
viewing status and availability above any progress bar, and the featured title
shows the resume episode once. Home Library updates surfaces changes and issues,
with routine checks in the full activity log.

For You opens on Anime, with separate Movies and TV Shows categories. Anime
excludes locally watched IDs and uses a bounded read of the connected AniList
watch list, including entries that have no Omakase monitor. If the history check
fails, the page displays a retry action before showing any anime picks.
For You allocates each anime to at most one shelf per screen. Narrow airing and
film shelves retain eligible candidates while personalized shelves rank by
viewing, ratings and genre. Already watched opens an optional score selector and
saves only on explicit submission.

The Settings landing page is a directory. Watching and account tasks come first.
Video quality uses familiar resolution names and a clear fallback order. More
detailed source rules are available under an Advanced download rules disclosure.
The form retains the same saved values and validation.

Movie watch history and rating save together with a clear receipt. A watched
movie can be reviewed in place through a read-only exact Plex identity check.
The review distinguishes a match, absence in the checked library and an
inconclusive check.
Any copy-management action happens in Plex. The Omakase record remains saved.

The optional movie cleanup preview confirms one Riven publication, one Plex
media part and one Real-Debrid torrent by exact identity and selected file size.
It checks household use, the Plex scan preference and current playback. The
browser receives the check result without the provider identifiers.
Omakase reserves an opaque acquisition tag with the requesting account before
submitting a movie. Cleanup ownership requires the same tag on the exact Riven
publication. Older submissions and imported copies remain protected.
The per-account movie cleanup preference defaults to keeping the copy. Saving
both watched and a manual rating records one cleanup intent for the owned
acquisition. The preference applies to future completions. Clearing either
field or turning the preference off cancels a queued intent.

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

Anime, Movies and TV share CollectionSearch: the same surface, keyline, corner, spacing and 48px controls. Scope is explicit; collection filtering differs from catalog discovery. Anime collection cards reserve three title lines with clipping and a full accessible name. Movie and TV browse titles use up to three natural lines, followed within 8px by their year. Viewing history and availability occupy separate full-width rows, avoiding narrow phone status columns. Never overlap text or call zero available episodes ready.

Movie and TV collection search lives in a native Search & filters disclosure.
It works without JavaScript and opens automatically for an active query or the
movie Requests view. The phone masthead pairs the page title with its optional
Explore action; the decorative kicker and supporting introduction yield space
to the first complete poster row and its titles above the bottom navigation.

TV cards use observed season counts; only reconciled released sets establish completeness. Unknown coverage stays unknown. Removal is a separate confirmed library action naming actual destinations and retained records. Shared assets remain protected.

Movie and TV browse cards lead with title, year, recorded progress and one
availability line. Available movies include the known 4K, 1080p or 720p resolution
on that line. The movie detail page retains the explicit HD fallback explanation.
An absent viewing record adds no repeated label and never becomes an unwatched
claim. TV inventory counts, observation times and season coverage live in one
collapsed Library availability details section below the collection. Request
failures, restrictions and pending states remain visible on movie cards.

## Interaction and reach

Minimum touch targets 44px. Keyboard focus is visible; disclosures and menus support native keyboard behavior. Shelves use native momentum and snapping on touch, clickable arrows on desktop. Phone navigation respects safe-area insets. 320px and 200% text must remain readable without page overflow.

## Proof

Technical pass, rendered creative acceptance, production identity and physical-device acceptance are separate. Check true landscape, portrait, tiny and missing art; real long titles; stale/partial season evidence; removal states using isolated fixtures. Runtime design tokens are maintained in the CSS under src/omakase/plus/static.

## Notification center

The Account utility group includes a 44px notification bell. It uses one deferred, deadline-bound same-origin local-DB read per page; there is no polling or provider lookup. Unknown/loading/error never claims zero unread. The compact nonmodal panel uses the existing Geist/midnight/iris system, 14px corners, 14–16px body/title text, current saved rating, and neutral media-type art placeholders when no cached artwork is provided.

Primary links open the existing rating or guarded removal-review flow. Each completed title has one card. When both reminders apply, it offers separate Rate title and Review media actions, with one unread/dismiss state and a compact 44px Dismiss cross. Saving a rating keeps the media-review action; a genuinely new completion or managed asset can create a fresh reminder. Primary-link activation marks read through a nonblocking CSRF-protected keepalive POST; dismissal is a separate account-scoped action. Both use the returned feed without another GET. The panel supports Close, Escape, outside dismissal and focus return, and closes when keyboard focus moves outside. Phones use viewport-bounded height, an independently scrolling list and safe-area gutters. Readiness and saved progress remain distinct; a reminder performs no import, provider call, acquisition or removal.

A pending household title request shows the title and requester's household name
to the owner. Review request links to the corresponding decision card in
Household settings. The card shows Pending, Approved, Declined or Cancelled.
The member sees the outcome in their own request list. Permission controls live
under each member and keep the owner's request queue separate from acquisition.

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

Devices separates personal access from shared household devices. The owner sees
shared rows grouped by household member, with device name, last activity,
masked public network when available, and expiry. Device names can include a
room or place. Sharing a row gives the owner visibility into those details.

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

Episode cards lead with artwork, episode name and watch progress. Amber
identifies 720p fallback copies. Missing, upcoming and removed media remain
visible. Normal verification and release quality live in one Files & quality
disclosure below the episode grid. Release claims remain labeled release quality.
Measured dimensions require current source, exact file and episode proof. A new
higher release cannot hide the still-selected fallback. Phones stack the section
heading and action with breathing room; watch controls span the full title grid.
Anime episode browsing uses two columns on phones, with complete episode names
and separate 44px playback targets.
Routine progress-source and AniList synchronization notes sit inside Watch
progress & rating. Failed sync and conflicting progress remain visible by the
hero's progress summary.

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

## Series detail covers and episode cards

Anime episode stills have no Unwatched overlay. Watched, Up next and saved
remaining time stay independent from media verification. The exact playback
action appears beside the episode number below the still, with a 44px keyboard
and touch target. The still is also clickable; its auxiliary link stays outside
the tab order and accessibility tree. Hover or focus within the episode reveals
the central play cue without covering every resting still. TV keeps its existing
episode presentation. Episode file and quality evidence lives inside the single
Media availability & playback disclosure, whose summary reports overall verified
coverage once. Per-release and per-episode evidence remains separately labeled.

Personal Anime and TV detail pages lead with a bounded landscape cover and an
exact episode action when the existing player can open that target. Continue
Watching follows a source-bound saved position or the next known episode after
recorded viewing. Synopsis, rating and inventory remain separate from playback.
On phones the cover text and actions stack, and the episode grid reflows to fit.

Episode cards pair a stable 16:9 still with a readable number, available episode
title, and independent viewing and media facts. Watched comes from recorded
episode progress. An exact saved playhead can show time remaining and a progress
bar. Qualified Anime season metadata supplies names and stills through the owned
Plex identity. TV uses exact catalog season and episode keys. Still reads happen
near the viewport and share bounded season metadata for legacy rows. A loaded
series fallback says Series artwork. While stills load or when art is missing,
the frame displays the Omakase Plus seal and wordmark. Missing art also retains
an accessible episode-specific description. Play, replay and TV watch-record buttons retain their
existing scoped routes and keyboard targets.

## Continue watching progress

Continue Watching cards use the same series watched count and bar, including
zero watched episodes when the total is known. A saved episode position stays
separate: the episode line shows its remaining minutes, or Up next when there
is no saved position. Do not replace the series bar with an unlabeled episode
percentage. Unknown totals do not invent a series percentage. Keep the episode
line visible and card footers aligned at normal text sizes; enlarged text can
wrap without overflowing. The home recommendation action also wraps at200% text.

The Continue Watching thumbnail cue uses a compact dark backing. Its action and
episode text keep at least4.5:1 contrast over white artwork, while the upper60%
of the image remains unobstructed. The entire cover retains the playback target.

## Player transport and episode metadata

The web player exposes guarded ten-second seek steps, Audio and Subtitles
directly in its transport. Next episode names the action explicitly; Quality,
playback speed and stream information remain in Settings. A dark lower backing
protects controls and timecodes from bright footage. Playing video can hide idle
chrome, while paused video, keyboard focus and open menus keep it visible.

On phones, one compact Back/header row keeps video near the top. Apple TV linking
lives inside Playback options. Audio and Subtitles share the secondary control
baseline; enlarged text can reflow them. Subtitle canvas bounds follow the video,
including its intrinsic aspect, rather than the complete control frame.

Desktop track menus clear the transport and suppress its duplicate tooltips.
On phones, the open menu reserves measured space after the player so it covers
neither video nor episode details. The seek bar remains usable. Closing the menu
restores the original layout; the video and subtitle canvas keep their bounds.
Next episode stays in the transport. See episodes remains visible below, with
optional adjacent links in a collapsed disclosure. Desktop playback recovery
removes the empty stage so the recovery action and episode context remain visible.

Track labels use readable language names and retain exact selection identities.
An unknown language stays Unknown language. Prepared, HLS and on-demand responses
reuse the resolved episode name and summary, without another metadata request.
The detail heading names that episode; a missing episode synopsis labels the
fallback About the series. Display metadata never changes the saved source key.

TV episode cards show exact titles or an Episode N fallback, a known runtime,
and a short synopsis when the scoped metadata provides them. Unknown runtimes
stay absent. The existing owned Plex episode read can enrich the native catalog;
metadata does not establish availability, viewing or successful playback.
Native pre-play uses one artwork background with a protected text region and an
inline runtime/audio/subtitle row when it fits. Libraries retains the same label
across scopes. Device Settings fills the screen and isolates its destructive
confirmation from the library behind it.

The broken-circle plus is the shared brand emblem for the web header and Home
hero, and for Apple TV navigation, setup, device settings, and artwork fallbacks.
When a native feature has no artwork reference, use a compact banner with the
emblem on the same uninterrupted surface. A feature with artwork retains its
cinematic composition; image scaling does not add a second artwork request.

Primary web actions use the iris fill with dark text. Secondary actions retain
their quieter treatment. Web disclosures use a small chevron and a rounded
visible focus ring on the summary controls. Keep
the settings navigation independently scrollable on desktop, with enough room
below the final link for its focus ring, and horizontally scrollable on phones.
