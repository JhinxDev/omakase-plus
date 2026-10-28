# Existing series adoption

Use this recovery flow when the correct anime is already fully downloaded in
Real-Debrid but is not represented by an Omakase monitor asset. It creates no
torrent and never selects or deletes Real-Debrid files.

Create a private request file:

```json
{
  "schema_version": "existing-series-adoption-request-v1",
  "requests": [
    {
      "monitor_id": 123,
      "torrent_hash": "0123456789ABCDEF0123456789ABCDEF01234567",
      "quality": "1080p"
    }
  ]
}
```

Plan first:

```bash
omakase plus plan-existing-series \
  --user-id 1 \
  --request /secure/request.json \
  --plan /secure/plan.json \
  --data-dir /home/omakase/data
```

Review and approve the printed plan hash out of band, then apply it with a new
backup path:

```bash
omakase plus apply-existing-series \
  --plan /secure/plan.json \
  --backup /secure/backups/omakase-plus-pre-series-adoption.db \
  --expected-plan-hash <reviewed-sha256> \
  --data-dir /home/omakase/data
```

The planner requires a matching target/season identity, a fully downloaded
torrent, and exactly one unambiguous episode per selected media file. The apply
step verifies the plan hash, re-fetches the exact BTIH and file inventory,
rejects concurrent database drift, creates and validates a SQLite rollback, and
records the asset as externally owned. Curator publication still fails closed
unless each manifest-managed episode has a release-matched English subtitle
track that can be extracted and validated.

When the provider's current display name is abbreviated, the planner may use a
prior successful download-attempt title as identity evidence only when the
owner, AniList ID, and exact BTIH all match. The evidence title is included in
the hash-bound plan and re-observed before apply. Conflicting or wrong-season
provider titles never receive this fallback.
