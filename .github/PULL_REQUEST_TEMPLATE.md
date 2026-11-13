## Summary

Describe the user-visible change and why it belongs in this release.

## Verification

- [ ] `python -m pytest -q` passes locally
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] User-visible documentation or changelog updated when behavior changed
- [ ] No secrets, account data, private addresses or generated artifacts added

## Risk and rollback

Describe the blast radius, migration or configuration impact, and the exact
rollback path.

## Live verification (release or deployment PRs)

- [ ] Built image revision matches the source revision
- [ ] Health and security checks verified against the deployed target
- [ ] Backup and rollback path retained
