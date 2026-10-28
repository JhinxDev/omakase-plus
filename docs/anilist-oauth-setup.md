# AniList OAuth Setup

Omakase Plus uses AniList's OAuth Authorization Code flow with PKCE to manage
entries in your **My Anime List**. It can add a title to Planning or mark a
recommendation `COMPLETED` with an optional score.

## Register an OAuth App

1.  Go to <https://anilist.co/settings/developer> and sign in to your
    AniList account.

2.  Click **Create New Client**.

3.  Fill in the form:
    - **Name** — anything meaningful (e.g. "Omakase Plus").
    - **Redirect URL** — set this to:
      ```
      http://localhost:8766/plus/integrations/anilist/callback
      ```
      (If you run Omakase on a different host or port, adjust the URL
      accordingly. The path `/plus/integrations/anilist/callback` must
      remain the same.)

4.  Click **Save**.

5.  Copy the **Client ID** and **Client Secret** shown on the developer
    page.

## Add Credentials to Your Environment

Add these to your `.env` file (or export them in your shell):

```env
ANILIST_CLIENT_ID=your_client_id_here
ANILIST_CLIENT_SECRET=your_client_secret_here
OMAKASE_PLUS_URL=http://localhost:8766
```

- `ANILIST_CLIENT_ID` — from the developer page.
- `ANILIST_CLIENT_SECRET` — from the developer page.
- `OMAKASE_PLUS_URL` — the base URL where your Omakase Plus server is
  running (defaults to `http://localhost:8765` if unset). Portable Compose uses
  port `8766`. The redirect
  URL you registered must match `<OMAKASE_PLUS_URL>/plus/integrations/anilist/callback`.

## Connect in the UI

1.  Start the Omakase Plus server with `OMAKASE_PLUS_PRIVATE=true`.
2.  Navigate to **Settings** (`/plus/settings`).
3.  Click **Connect AniList** — you will be redirected to AniList to
    authorize the application.
4.  After authorizing, you will be redirected back and the OAuth token
    will be stored encrypted at rest.

The handshake is owner-scoped, single-use, expires after ten minutes, and is
stored server-side so a restart or another web worker does not invalidate a
valid callback. Do not put the Client Secret or access token in the non-secret
environment file.

## Verify a My Anime List write

From Discover, choose **Already watched**, optionally select a 1–10 score, and
save. Activity must report an AniList list update as synced. Confirm the title
appears as Completed on AniList. If the tracker is unavailable, Activity keeps
the safe retry; a local recommendation hide is never presented as a successful
remote write.
