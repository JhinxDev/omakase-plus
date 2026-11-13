# Optional Google sign-in

Google can be linked to an existing authenticated Omakase account. The password
remains usable. An unlinked Google identity cannot create or claim an app account
by matching an email address. Linking changes no media or tracking-provider data.

## Configure your deployment

Create your own Web application OAuth client in Google Cloud and configure its
consent screen and intended audience. Register the exact HTTPS callback ending
`/plus/auth/google/callback` for your protected app origin. Supply GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI through your protected deployment
configuration. Do not commit the downloaded client file or secret values.

The implementation uses authorization-code flow, S256 PKCE, state, nonce and a
Secure/HttpOnly/SameSite=Lax binding cookie. The scope is `openid`; no Drive,
Calendar, offline-access or list-provider permissions are requested. HTTP callback
URLs, credentials in URLs, query strings, fragments and mismatched paths fail closed.

## Link and verify

1. Sign in with the existing password and open Google sign-in in Account settings.
2. Choose Link Google account and authenticate with the intended Google identity.
3. Confirm the linked state, sign out, and use Continue with Google.
4. Verify that the same app account, watch history and ratings remain present.
5. Keep password access available. Unlink in Account settings when desired;
   this disables later Google sign-ins without revoking the Google account itself.

Use isolated fixtures for development. Actual OAuth activation, audience changes
and sign-in acceptance belong to the administrator of each installation. No
deployment-specific project, account, callback or historical receipt is included.
