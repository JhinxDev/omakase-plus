# Security

Omakase Plus is early self-hosted software. No broad production-readiness or
security certification is claimed. Keep the application authenticated, bind to
loopback by default and use a protected HTTPS origin for deliberate remote access.

## Report a vulnerability

The source repository is JhinxDev/omakase-plus. If private vulnerability
reporting is enabled there, use its Security tab. Otherwise request a private
reporting channel without posting credentials, personal data or exploit details
in a public issue. Do not test against someone else's installation.

## Installation and maintenance

Keep encryption keys, OAuth credentials and provider tokens outside source
control. Back up the database together with its protected encryption key; a
database backup alone cannot recover encrypted connection settings. Restrict
backup access and verify recovery on an isolated copy.

Use exact source revisions and the locked dependencies. Review migrations and
release checks before upgrading. OMAKASE_PLUS_PRIVATE enables the authenticated
application and must not be disabled to make public source easier to deploy.

Only connect services and media you are authorized to use. Shared/adopted media,
watch history and ratings have independent preservation rules. Removal requires
explicit scope and confirmation; provider restrictions are not permission to bypass them.
