# Omakase Plus design contract

This source candidate records the maintained interface contract. Technical, creative and deployment acceptance remain separate.

## Personal cinema

Artwork leads; one primary action follows. Geist is served locally. Pale iris carries selection/action, emerald confirmed availability, amber partial/attention, slate unknown, muted red destructive/failure. Every state has meaningful words as well as color. Availability, watching, rating and media verification are independent.

Home uses a 64–72vh cinematic stage, a genuine wide image, long text-protection gradient and bottom fade. Preserve the artwork's midtones. Inadequate landscape images fall back to a crisp source-sized portrait inside a broad blurred atmosphere from the same title. Blur is an intentional color field, never a claim of sharp source detail. Never leave one small portrait floating on flat black. No automatic rotation. Phone art leads above a compact text block. Reduced motion is fully composed immediately.

## Shared components

Anime, Movies and TV share CollectionSearch: the same surface, keyline, corner, spacing and 48px controls. Scope is explicit; collection filtering differs from catalog discovery. Dense cards reserve three title lines with clipping and full accessible name, then use normal-flow metadata, progress, availability and quality rows. Never overlap text or call zero available episodes ready.

TV cards use observed season counts; only reconciled released sets establish completeness. Unknown coverage stays unknown. Removal is a separate confirmed library action naming actual destinations and retained records. Shared assets remain protected.

## Interaction and reach

Minimum touch targets 44px. Keyboard focus is visible; disclosures and menus support native keyboard behavior. Shelves use native momentum and snapping on touch, clickable arrows on desktop. Phone navigation respects safe-area insets. 320px and 200% text must remain readable without page overflow.

## Proof

Technical pass, rendered creative acceptance, production identity and physical-device acceptance are separate. Check true landscape, portrait, tiny and missing art; real long titles; stale/partial season evidence; removal states using isolated fixtures. Runtime design tokens are maintained in the CSS under src/omakase/plus/static.

## Notification center

The Account utility group includes a 44px notification bell. It uses one deferred, deadline-bound same-origin local-DB read per page; there is no polling or provider lookup. Unknown/loading/error never claims zero unread. The compact nonmodal panel uses the existing Geist/midnight/iris system, 14px corners, 14–16px body/title text, current saved rating, and neutral media-type art placeholders when no cached artwork is provided.

Primary links open the existing rating or guarded removal-review flow. Each item has one primary Rate/Review media link and a compact 44px Dismiss cross. Primary-link activation marks read through a nonblocking CSRF-protected keepalive POST; dismissal is a separate account-scoped action. Both use the returned feed without another GET. The panel supports Close, Escape, outside dismissal and focus return, and closes when keyboard focus moves outside. Phones use viewport-bounded height, an independently scrolling list and safe-area gutters. Readiness and saved progress remain distinct; a reminder performs no import, provider call, acquisition or removal.
