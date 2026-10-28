# Audio and subtitle settings

**Next batch · pending release qualification.** These source instructions are not
part of the deployed `.3` release yet. Feature flags remain off until the exact
candidate and combined migration/rollback checks pass. No owner/provider account
has been connected by these source changes.

## Choose, preview, save

1. Select the intended library, then open **Settings → Download criteria → Audio
   and subtitles**. Choose Anime, Movies or TV.
2. Choose required languages and require-one or require-every matching. Open the
   optional preference/fallback controls only when needed.
3. Preview without saving, then save your personal preference or owner-controlled
   library default. Reset affects only that media type.

For example, require French and English audio in one compatible multi-track copy.
That preference does not prove either track exists. Verified physical inventory
establishes available tracks; a verified personal Plex account binding establishes
which track that viewer selected. Unknown selection is never guessed from the file.


The private Settings → Download criteria page links to `/plus/settings/languages`
when `OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED=true` and `OMAKASE_PLUS_PRIVATE=true`.
The settings flag defaults off. It is independent of household navigation and of
verified language publication capability. Saving choices never enables acquisition,
creates track observations or makes a file ready. TV remains catalog/tracking only.

Choose Anime, Movies or TV, then select required languages and require-one or
require-every matching. Preferences and alternative fallback languages are in an
optional disclosure. Full dialogue is required for subtitle satisfaction; forced
or signs-only tracks cannot substitute. SDH and selectable/burned-in policies are
explicit. Unknown actual tracks remain unverified. Region-specific language choices
are preserved. Choose up to eight supported codes per list through displayed names;
unsupported/arbitrary policy fields are rejected.

The selected library is resolved from the signed-in account. Owners can change
that library's defaults and member-choice permissions; members can save only their
own preferences when allowed. The form binds library, membership and policy
revisions, rejects stale submissions, uses CSRF and bounded form parsing, and
returns private/no-store responses. A preview performs no persistence or provider
request. Reset is explicit and affects only the current domain: the library's
original policy for owners, or deletion of the viewer's preference. Existing quality
criteria, saved credentials, other users and physical media are unchanged.

Original policies remain the legacy policies rather than being silently replaced
by new-install recommendations: anime has unconstrained audio with English
full-dialogue subtitles required; movies require English audio; TV has no required
language. `DomainLanguagePolicies.new_defaults()` is not substituted by this UI.

## Preference API and request boundary

`language_preferences.member_preference(db, scope, domain)` reads only the viewer's
canonical `LanguageRule` and validates its revision. `set_member_preference(...,
rule)` revalidates current library permissions inside a transaction; `None` removes
only that viewer/library/domain preference. `effective_language(db, scope, domain)`
resolves current library defaults and the permitted saved preference into the
existing typed `EffectiveLanguagePolicy`. Disabled preferences remain saved but do
not apply. The additive `member_language_preferences` table in migration034 contains
rule choices only, with library/user foreign keys and no provider/verification data.

Intake must freeze that effective rule into its existing immutable
`LanguageRequestSnapshot`, including actor/library/domain, inherited revision and
exact item identity. A Settings save does not rewrite existing snapshots. Dispatch
must revalidate the snapshot, membership, configured source and separately qualified
publication contract. Preferences are not proof of content, ownership or readiness.
The settings flag alone must never authorize legacy acquisition or replacement of
another member's working media. Combined migrations 033–036 app/worker/curator qualification
and rollback remain separate release gates.

## Focused offline checks

Use the existing environment; no packages or provider accounts are needed:

```powershell
.venv/Scripts/python.exe -m pytest tests/plus/test_language_settings.py -q
node tests/js/language_settings_qa.cjs --python <existing-python> --modules <existing-node-module-root> --browser <existing-edge-executable> --output <synthetic-evidence-directory>
```

The maintained browser harness renders the actual template/CSS with synthetic
libraries and intercepts every request. It checks owner/member/locked/preview/error/TV
at 320/390/1440, expanded disclosures, keyboard focus, labelled controls, contrast,
no page overflow, no nested forms and a zero-real-provider preview form journey.
Authentication, CSRF, scope isolation, stale permissions, reset, closed validation
and absence of evidence writes are tested through actual authenticated routes.
Browser emulation does not prove physical iPhone native multi-select behavior.
