# Protected application configuration

Keep deployment secrets in your own protected configuration or password manager.
Never commit generated values, account exports, real .env files or databases.
The checked-in .env example files contain variable names and placeholders only.

| Variable | Purpose |
| --- | --- |
| OMAKASE_PLUS_MASTER_KEY | Master encryption key for saved account connections; generate as described in the getting-started guide |
| OMAKASE_SEED_EMAIL / OMAKASE_SEED_PASSWORD | Optional administrator bootstrap credentials |
| ANILIST_CLIENT_ID / ANILIST_CLIENT_SECRET | Your optional AniList OAuth client |
| GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI | Your optional Google sign-in client and exact HTTPS callback |
| OMAKASE_LIBRARY_MANIFEST_TOKEN | Dedicated machine credential for optional curator manifests/receipts |

Configure media-provider credentials in the authenticated application settings
where supported. A missing optional connection remains unavailable; no default
account, secret or provider subscription is supplied.

OMAKASE_PLUS_PRIVATE=true enables authenticated Plus routes. The unset/false
mode is the separate recommendation-only surface; it is not a way to remove
authentication from a Plus installation. Public source visibility does not change
this runtime access boundary.

Back up the database and protected encryption key using independent protected
records, and test recovery on an isolated copy. See [Getting started](getting-started.md),
[Google sign-in](google-signin.md), and [Security](../SECURITY.md).
