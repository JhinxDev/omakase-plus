"""Pretty terminal output using Rich."""

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from omakase.types import Recommendation

console = Console()


def show_recommendations(recs: list[Recommendation], source: str, username: str) -> None:
    """Print recommendations as a formatted table."""
    if not recs:
        console.print(
            "[yellow]No recommendations were returned. Check the LLM output above.[/yellow]"
        )
        return

    table = Table(
        title=f"Recommendations for {username} ({source})",
        box=box.ROUNDED,
        border_style="purple",
        title_style="bold purple",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Title", style="bold", width=40)
    table.add_column("Score", justify="center", width=6)
    table.add_column("Similar to", style="cyan", width=25)
    table.add_column("Why", width=60)

    for i, rec in enumerate(recs, 1):
        score_color = (
            "green" if rec.predicted_score >= 8 else "yellow" if rec.predicted_score >= 6 else "red"
        )
        table.add_row(
            str(i),
            rec.title,
            f"[{score_color}]{rec.predicted_score:.0f}/10[/{score_color}]",
            rec.best_match_from_history,
            rec.reasoning,
        )

    console.print()
    console.print(table)
    console.print()


def show_summary(data_size: int, prompt_tokens: int, recs: list[Recommendation]) -> None:
    """Print a small summary after results."""
    console.print(
        Panel(
            f"[bold]Summary[/bold]\n"
            f"  Data source: {data_size} entries\n"
            f"  Prompt: ~{prompt_tokens} tokens\n"
            f"  Recommendations: {len(recs)}",
            box=box.SQUARE,
        )
    )
