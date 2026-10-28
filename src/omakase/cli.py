"""CLI entry point for Omakase."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from omakase import __version__
from omakase.adapters.base import list_sources
from omakase.engine import run
from omakase.output.terminal import show_recommendations
from omakase.types import OmakaseConfig, resolve_model_preset

LLM_TYPES = [
    "ollama",
    "lmstudio",
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "openrouter",
    "groq",
    "together",
]


@click.group()
@click.version_option(version=__version__)
def cli():
    """Omakase — an LLM-powered sommelier for anime.

    \b
    Connect AniList or MyAnimeList, point it at any LLM (local or cloud),
    and get a hand-picked tasting menu of anime based on what you actually
    love — not what's popular.
    """


@cli.command()
@click.option("--source", "-s", default="anilist", help="Data source (anilist, myanimelist)")
@click.option("--username", "-u", required=True, help="Your username on the source platform")
@click.option(
    "--profile",
    "-p",
    default="taste-profile.md",
    help="Path to your taste profile markdown file",
    show_default=True,
)
@click.option(
    "--llm-url",
    default="http://localhost:11434",
    help="LLM API base URL",
    show_default=True,
    envvar="OMAKASE_LLM_URL",
)
@click.option(
    "--model",
    "-m",
    default="qwen2.5:7b",
    help="Model name",
    show_default=True,
    envvar="OMAKASE_MODEL",
)
@click.option(
    "--llm-type",
    default="ollama",
    type=click.Choice(LLM_TYPES),
    help="LLM backend type",
    show_default=True,
)
@click.option(
    "--pool-size", default=100, help="Number of candidates to consider", show_default=True
)
@click.option(
    "--temperature", default=0.4, help="LLM temperature (0.0-1.0)", show_default=True, type=float
)
@click.option(
    "--json", "json_output", is_flag=True, help="Output raw JSON instead of formatted table"
)
@click.option(
    "--mode",
    default="fast",
    type=click.Choice(["fast", "pro"]),
    help="Model tier: fast (cheap) or pro (thinking)",
    show_default=True,
)
@click.option("--use-planning", is_flag=True, help="Use your Plan to Watch list as candidates")
def recommend(
    source,
    username,
    profile,
    llm_url,
    model,
    llm_type,
    pool_size,
    temperature,
    json_output,
    mode,
    use_planning,
):
    """Serve your anime tasting menu — 5–10 personalized picks."""
    llm_url, llm_type, model, supports_json = resolve_model_preset(llm_url, llm_type, model, mode)

    cfg = OmakaseConfig(
        source=source,
        username=username,
        llm_url=llm_url,
        model=model,
        profile_path=profile,
        candidate_pool_size=pool_size,
        temperature=temperature,
        llm_type=llm_type,
        output_format="json" if json_output else "terminal",
        mode=mode,
        supports_json_mode=supports_json,
        use_planning=use_planning,
    )

    recs = run(cfg)

    if json_output:
        print(
            json.dumps(
                {
                    "source": source,
                    "username": username,
                    "recommendations": [
                        {
                            "title": r.title,
                            "predicted_score": r.predicted_score,
                            "reasoning": r.reasoning,
                            "best_match_from_history": r.best_match_from_history,
                        }
                        for r in recs
                    ],
                },
                indent=2,
            )
        )
    else:
        show_recommendations(recs, source, username)


@cli.command()
def sources():
    """List available data source adapters."""
    click.echo("Available sources:")
    for s in list_sources():
        click.echo(f"  - {s}")


@cli.command()
def backends():
    """List available LLM backends."""
    from omakase.llm import list_backends

    click.echo("Available LLM backends:")
    for b in list_backends():
        click.echo(f"  - {b}")


@cli.group()
def plus():
    """Operate the private Omakase Plus runtime."""


from omakase.plus.movies_cli import movies_cli  # noqa: E402

plus.add_command(movies_cli)

from omakase.plus.agent_cli import agent_cli  # noqa: E402

plus.add_command(agent_cli)


@plus.command("sync-media")
def sync_media_command():
    """Reconcile every due media monitor once."""
    from omakase.plus import media_cli

    try:
        receipt = media_cli.sync_media_once()
    except Exception as exc:
        raise click.ClickException("Media scheduler failed") from exc
    click.echo(f"Media scheduler: {receipt.outcome}")


@plus.command("plan-existing-movies")
@click.option("--user-id", required=True, type=click.IntRange(min=1))
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--plan", "plan_path", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--data-dir", default="data", show_default=True)
def plan_existing_movies_command(user_id, request_path, plan_path, data_dir):
    """Create a read-only, hash-bound plan for existing RD movies."""
    from omakase.plus import media_cli

    try:
        plan = media_cli.plan_existing_movies(
            user_id=user_id,
            request_path=request_path,
            plan_path=plan_path,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Movie adoption planning failed") from exc
    click.echo(f"Movie adoption plan: {plan['plan_hash']} ({len(plan['movies'])} movies)")


@plus.command("apply-existing-movies")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--backup",
    "backup_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--expected-plan-hash", required=True)
@click.option("--data-dir", default="data", show_default=True)
def apply_existing_movies_command(plan_path, backup_path, expected_plan_hash, data_dir):
    """Revalidate and adopt an approved movie plan after a verified backup."""
    from omakase.plus import media_cli

    try:
        result = media_cli.apply_existing_movies(
            plan_path=plan_path,
            backup_path=backup_path,
            expected_plan_hash=expected_plan_hash,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Movie adoption apply failed") from exc
    click.echo(
        f"Movie adoption applied: {result['plan_hash']} "
        f"({result['movies']} movies, {result['assets']} assets)"
    )


@plus.command("plan-existing-series")
@click.option("--user-id", required=True, type=click.IntRange(min=1))
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--plan", "plan_path", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--data-dir", default="data", show_default=True)
def plan_existing_series_command(user_id, request_path, plan_path, data_dir):
    """Create a read-only, hash-bound plan for existing RD series."""
    from omakase.plus import media_cli

    try:
        plan = media_cli.plan_existing_series(
            user_id=user_id,
            request_path=request_path,
            plan_path=plan_path,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Series adoption planning failed") from exc
    click.echo(f"Series adoption plan: {plan['plan_hash']} ({len(plan['series'])} series)")


@plus.command("apply-existing-series")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--backup",
    "backup_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--expected-plan-hash", required=True)
@click.option("--data-dir", default="data", show_default=True)
def apply_existing_series_command(plan_path, backup_path, expected_plan_hash, data_dir):
    """Revalidate and adopt an approved series plan after a verified backup."""
    from omakase.plus import media_cli

    try:
        result = media_cli.apply_existing_series(
            plan_path=plan_path,
            backup_path=backup_path,
            expected_plan_hash=expected_plan_hash,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Series adoption apply failed") from exc
    click.echo(
        f"Series adoption applied: {result['plan_hash']} "
        f"({result['series']} series, {result['assets']} assets, "
        f"{result['episodes']} episodes)"
    )


@plus.command("plan-local-import")
@click.option("--user-id", required=True, type=click.IntRange(min=1))
@click.option(
    "--root",
    "root_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--plan", "plan_path", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--data-dir", default="data", show_default=True)
def plan_local_import_command(user_id, root_path, request_path, plan_path, data_dir):
    """Observe local media and write a hash-bound, non-mutating import plan."""
    from omakase.plus import media_cli

    try:
        plan = media_cli.plan_local_media(
            user_id=user_id,
            root_path=root_path,
            request_path=request_path,
            plan_path=plan_path,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Local import planning failed") from exc
    click.echo(
        f"Local import plan: {plan['plan_hash']} "
        f"({len(plan['files'])} files; no files or database rows changed)"
    )


@plus.command("apply-local-import")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--root",
    "root_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--backup",
    "backup_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--expected-plan-hash", required=True)
@click.option("--data-dir", default="data", show_default=True)
def apply_local_import_command(
    plan_path,
    root_path,
    backup_path,
    expected_plan_hash,
    data_dir,
):
    """Back up SQLite and apply an approved local-media import plan."""
    from omakase.plus import media_cli

    try:
        receipt = media_cli.apply_local_media(
            plan_path=plan_path,
            root_path=root_path,
            backup_path=backup_path,
            expected_plan_hash=expected_plan_hash,
            data_dir=data_dir,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Local import apply failed") from exc
    click.echo(
        f"Local import applied: {receipt['plan_hash']} "
        f"({receipt['usable']} usable, {receipt['quarantined']} quarantined, "
        f"{receipt['already_present']} already present)"
    )


@plus.command("plan-plex-setup")
@click.option("--plex-url", required=True, envvar="OMAKASE_PLEX_URL")
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--plan", "plan_path", required=True, type=click.Path(path_type=Path, dir_okay=False))
def plan_plex_setup_command(plex_url, request_path, plan_path):
    """Read Plex and write a non-destructive, hash-bound library plan."""
    from omakase.plus import media_cli

    token = os.getenv("OMAKASE_PLEX_TOKEN", "")
    if not token:
        raise click.ClickException("Set OMAKASE_PLEX_TOKEN in the private environment")
    try:
        plan = media_cli.plan_plex_libraries(
            plex_url=plex_url,
            plex_token=token,
            request_path=request_path,
            plan_path=plan_path,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Plex setup planning failed") from exc
    click.echo(
        f"Plex setup plan: {plan['plan_hash']} ({len(plan['actions'])} non-destructive actions)"
    )


@plus.command("apply-plex-setup")
@click.option("--plex-url", required=True, envvar="OMAKASE_PLEX_URL")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--expected-plan-hash", required=True)
def apply_plex_setup_command(plex_url, plan_path, expected_plan_hash):
    """Apply an approved Plex create/path-update plan and request scoped scans."""
    from omakase.plus import media_cli

    token = os.getenv("OMAKASE_PLEX_TOKEN", "")
    if not token:
        raise click.ClickException("Set OMAKASE_PLEX_TOKEN in the private environment")
    try:
        receipt = media_cli.apply_plex_libraries(
            plex_url=plex_url,
            plex_token=token,
            plan_path=plan_path,
            expected_plan_hash=expected_plan_hash,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Plex setup apply failed") from exc
    click.echo(
        f"Plex setup applied: {receipt['plan_hash']} "
        f"({receipt['actions_applied']} actions, {receipt['scans_requested']} scans)"
    )


def _doctor_config(
    *,
    data_dir,
    import_root,
    library_root,
    acquisition_mode,
    vpn_mode,
    plex_url,
    qbittorrent_url,
    external_vpn_interface,
):
    from omakase.plus.doctor import DoctorConfig

    return DoctorConfig(
        data_dir=Path(data_dir).resolve(),
        import_root=Path(import_root).resolve(),
        library_root=Path(library_root).resolve(),
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        acquisition_mode=acquisition_mode,
        plex_url=plex_url,
        plex_token=os.getenv("OMAKASE_PLEX_TOKEN", ""),
        qbittorrent_url=qbittorrent_url,
        qbittorrent_username=os.getenv("OMAKASE_QBITTORRENT_USERNAME", ""),
        qbittorrent_password=os.getenv("OMAKASE_QBITTORRENT_PASSWORD", ""),
        qbittorrent_category=os.getenv("OMAKASE_QBITTORRENT_CATEGORY", "omakase"),
        qbittorrent_save_root=os.getenv(
            "OMAKASE_QBITTORRENT_SAVE_ROOT",
            "/downloads/omakase",
        ),
        vpn_mode=vpn_mode,
        external_vpn_interface=external_vpn_interface,
    )


def _doctor_options(command):
    options = [
        click.option(
            "--external-vpn-interface", envvar="OMAKASE_EXTERNAL_VPN_INTERFACE", default=""
        ),
        click.option("--qbittorrent-url", envvar="OMAKASE_QBITTORRENT_URL", default=""),
        click.option("--plex-url", envvar="OMAKASE_PLEX_URL", default=""),
        click.option(
            "--vpn-mode",
            type=click.Choice(["none", "gluetun", "external"]),
            envvar="OMAKASE_VPN_MODE",
            default="none",
            show_default=True,
        ),
        click.option(
            "--acquisition-mode",
            type=click.Choice(["import-only", "real-debrid", "qbittorrent"]),
            envvar="OMAKASE_ACQUISITION_MODE",
            default="import-only",
            show_default=True,
        ),
        click.option(
            "--library-root",
            envvar="OMAKASE_LIBRARY_ROOT",
            default="/media/library",
            show_default=True,
        ),
        click.option(
            "--import-root",
            envvar="OMAKASE_IMPORT_ROOT",
            default="/media/import",
            show_default=True,
        ),
        click.option("--data-dir", envvar="OMAKASE_DATA_DIR", default="data", show_default=True),
    ]
    for option in reversed(options):
        command = option(command)
    return command


@plus.command("doctor")
@click.option("--json", "json_output", is_flag=True, help="Emit the stable JSON report")
@_doctor_options
def doctor_command(
    json_output,
    data_dir,
    import_root,
    library_root,
    acquisition_mode,
    vpn_mode,
    plex_url,
    qbittorrent_url,
    external_vpn_interface,
):
    """Run bounded, read-only storage, Plex, provider, and VPN checks."""
    from omakase.plus.doctor import report_payload, run_doctor

    try:
        report = run_doctor(
            _doctor_config(
                data_dir=data_dir,
                import_root=import_root,
                library_root=library_root,
                acquisition_mode=acquisition_mode,
                vpn_mode=vpn_mode,
                plex_url=plex_url,
                qbittorrent_url=qbittorrent_url,
                external_vpn_interface=external_vpn_interface,
            )
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Doctor failed") from exc
    if json_output:
        click.echo(json.dumps(report_payload(report), indent=2, sort_keys=True))
    else:
        for check in report.checks:
            click.echo(f"{check.status.upper():4}  {check.name}: {check.detail}")
        click.echo("READY" if report.ready else "NOT READY")
    if not report.ready:
        raise click.ClickException("readiness checks failed")


@plus.command("support-bundle")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
)
@_doctor_options
def support_bundle_command(
    output_path,
    data_dir,
    import_root,
    library_root,
    acquisition_mode,
    vpn_mode,
    plex_url,
    qbittorrent_url,
    external_vpn_interface,
):
    """Create a redacted support ZIP without database, names, or watch history."""
    from omakase.plus.doctor import run_doctor
    from omakase.plus.support import create_support_bundle

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        report = run_doctor(
            _doctor_config(
                data_dir=data_dir,
                import_root=import_root,
                library_root=library_root,
                acquisition_mode=acquisition_mode,
                vpn_mode=vpn_mode,
                plex_url=plex_url,
                qbittorrent_url=qbittorrent_url,
                external_vpn_interface=external_vpn_interface,
            )
        )
        receipt = create_support_bundle(
            output_path,
            report=report,
            app_version=__version__,
            created_at=now,
        )
    except Exception as exc:
        raise click.ClickException(str(exc) or "Support bundle failed") from exc
    click.echo(
        f"Support bundle: {receipt['path']} ({receipt['bytes']} bytes, sha256 {receipt['sha256']})"
    )


@cli.command()
@click.argument("path", type=click.Path(), default="taste-profile.md")
def init(path):
    """Create a starter taste profile."""
    template = """# My Taste Profile

## Things I love
- Morally complex protagonists, not pure-hearted heroes
- Dense world-building over slice-of-life
- Stories that earn their ending — strong third act
- [Add more...]

## Things I bounce off
- [Add what you dislike...]

## Characters that resonate
- [Character] — [why they resonate]

## Recent loves (don't recommend these — just calibration)
- [Title you scored highly]
"""
    dest = Path(path)
    if dest.exists():
        click.echo(f"File already exists: {path}", err=True)
        sys.exit(1)
    dest.write_text(template.strip() + "\n")
    click.echo(f"Created taste profile at {path}")
    click.echo(
        "Edit it to describe what you love, what you bounce off, and the characters that resonate with you."
    )


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option(
    "--port", "-p", default=8765, help="Port to listen on", show_default=True, envvar="OMAKASE_PORT"
)
def web(host, port):
    """Launch the Omakase web setup UI in your browser."""
    from omakase.web.server import run_server

    run_server(host=host, port=port)


if __name__ == "__main__":
    cli()
