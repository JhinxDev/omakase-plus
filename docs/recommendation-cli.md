# Recommendation CLI and web app

This is Omakase's recommendation-only workflow. It does not install or configure
the authenticated Plus media application. Use Python 3.11+ and a reviewed source checkout
with its committed lockfile:

```sh
uv sync --frozen
uv run --frozen omakase init
uv run --frozen omakase recommend -u your-anilist-handle
uv run --frozen omakase web
```

`init` creates a starter `taste-profile.md`. Describe what you enjoyed, disliked,
dropped or paused, then refine it as you try recommendations. The local web app
binds to loopback by default. Keep your model key private.

List the actual available adapters/backends and inspect options:

```sh
uv run --frozen omakase sources
uv run --frozen omakase backends
uv run --frozen omakase recommend --help
```

Supported adapters include AniList and MyAnimeList. Model backends include local
Ollama/LM Studio and supported cloud providers. Configure your own credentials
through the documented environment or local configuration, then use
`--llm-type`, `--mode` and `--model` as supported by the current CLI help.
A cloud model run may incur usage charges. Do not put real keys in shell history
or Git. Recommendations do not themselves verify media availability or grant
rights to acquire content.

For authenticated media orchestration, start with the
[Plus getting-started guide](getting-started.md).
