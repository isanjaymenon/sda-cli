# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "rich>=13",
#     "typer>=0.12",
# ]
#
# ///
"""sda -- subdomain finder CLI.

Finds the subdomains of a domain using the free Subdomain API
(https://subdomain.app). No signup, no API key.

Self-contained uv script (PEP 723 inline metadata):

    uv run sda.py example.com
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

API_URL = "https://api.subdomain.app/v1/query"
USER_AGENT = "sda-cli/1.0 (+https://subdomain.app)"
RETRYABLE_STATUS = {429, 503}
MAX_DELAY = 30.0

__version__ = "1.0.0"

app = typer.Typer(
    name="sda",
    help="Find the subdomains of a domain via the free Subdomain API (subdomain.app).",
    add_completion=False,
)
err_console = Console(stderr=True)


class sdaError(Exception):
    """Fatal error carrying a user-facing message and a process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class QueryResult:
    domain: str
    count: int
    total: int
    subdomains: list[str] = field(default_factory=list)


_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_valid_domain(value: str) -> bool:
    """Rough DNS hostname check: dot-separated labels, alpha present, TLD >= 2 chars."""
    if not value or len(value) > 253 or "." not in value:
        return False
    labels = value.split(".")
    if len(labels[-1]) < 2:
        return False
    if not any(char.isalpha() for char in value):
        return False
    return all(_LABEL_RE.match(label) for label in labels)


def normalize_domain(raw: str) -> str:
    """Reduce URLs, 'www.'/'*.' prefixes, credentials, ports and paths to a bare domain."""
    value = raw.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.rsplit("@", 1)[-1]                  # credentials
    value = re.split(r"[/?#]", value, maxsplit=1)[0]  # path/query/fragment
    value = value.split(":", 1)[0]                    # port
    value = value.strip(".")
    if value.startswith("*."):
        value = value[2:]
    if value.startswith("www."):
        value = value[4:]
    if not is_valid_domain(value):
        raise sdaError(f"invalid domain {raw!r} - enter something like 'example.com'", exit_code=2)
    return value


def create_client(timeout: float) -> httpx.Client:
    """Build the API client (separate function so tests can inject a mock)."""
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """Backoff delay for the given retry attempt, honoring the Retry-After header."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), MAX_DELAY))
        except ValueError:
            pass
    return min(2.0**attempt + random.uniform(0.0, 0.5), MAX_DELAY)


def fetch_subdomains(
    client: httpx.Client,
    domain: str,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> QueryResult:
    """Query the Subdomain API, retrying HTTP 429/503 with exponential backoff."""
    if sleep is None:
        sleep = time.sleep
    attempt = 0
    while True:
        try:
            response = client.get(API_URL, params={"domain": domain})
        except httpx.TimeoutException as exc:
            raise sdaError(f"request timed out after {client.timeout.read:g}s") from exc
        except httpx.HTTPError as exc:
            raise sdaError(f"network error: {exc}") from exc

        if response.status_code == 200:
            break
        if response.status_code == 400:
            raise sdaError(f"the API rejected {domain!r} as an invalid domain (HTTP 400)")
        if response.status_code in RETRYABLE_STATUS and attempt < retries:
            delay = retry_delay(response, attempt)
            attempt += 1
            if warn is not None:
                warn(f"HTTP {response.status_code} - retry {attempt}/{retries} in {delay:.1f}s")
            sleep(delay)
            continue
        raise sdaError(f"API error: HTTP {response.status_code}")

    try:
        data = response.json()
        return QueryResult(
            domain=str(data["domain"]),
            count=int(data["count"]),
            total=int(data["total"]),
            subdomains=[str(item) for item in data["subdomains"]],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise sdaError(f"could not parse the API response: {exc}") from exc


def render_txt(result: QueryResult) -> str:
    if not result.subdomains:
        return ""
    return "\n".join(result.subdomains) + "\n"


def render_json(result: QueryResult) -> str:
    payload = {
        "domain": result.domain,
        "count": result.count,
        "total": result.total,
        "subdomains": result.subdomains,
    }
    return json.dumps(payload, indent=2) + "\n"


def render_csv(result: QueryResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["subdomain"])
    for subdomain in result.subdomains:
        writer.writerow([subdomain])
    return buffer.getvalue()


RENDERERS = {"txt": render_txt, "json": render_json, "csv": render_csv}


def pick_format(as_json: bool, as_csv: bool, output: Path | None) -> str:
    if as_json and as_csv:
        raise sdaError("choose only one of --json and --csv", exit_code=2)
    if as_json:
        return "json"
    if as_csv:
        return "csv"
    if output is not None and output.suffix.lower() in (".json", ".csv", ".txt"):
        return output.suffix.lower()[1:]
    return "txt"


def run_query(root: str, *, timeout: float, retries: int, quiet: bool) -> QueryResult:
    def warn(message: str) -> None:
        if not quiet:
            err_console.print(f"[yellow]warning:[/yellow] {message}")

    if quiet:
        with create_client(timeout) as client:
            return fetch_subdomains(client, root, retries=retries, warn=warn)
    with err_console.status(f"[cyan]Looking up {root} ...[/cyan]", spinner="dots"):
        with create_client(timeout) as client:
            return fetch_subdomains(client, root, retries=retries, warn=warn)


def emit(result: QueryResult, *, fmt: str, count_only: bool, output: Path | None, quiet: bool) -> None:
    text = f"{result.count}\n" if count_only else RENDERERS[fmt](result)

    if not quiet and not count_only:
        if result.count == 0:
            err_console.print(f"[yellow]No subdomains found for {escape(result.domain)}.[/yellow]")
        else:
            extra = ""
            if result.count < result.total:
                extra = f"truncated: {result.total} known, API returns at most 10000 | "
            err_console.print(
                Panel.fit(
                    f"[bold]{escape(result.domain)}[/bold]\n"
                    f"[green]{result.count} subdomain(s) found[/green]\n"
                    f"[dim]{extra}source: api.subdomain.app[/dim]",
                    border_style="cyan",
                )
            )

    if output is not None:
        output.write_text(text, encoding="utf-8")
        if not quiet:
            err_console.print(f"[green]Wrote {result.count} subdomain(s) to {output}[/green]")
    else:
        sys.stdout.write(text)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sda {__version__}")
        raise typer.Exit()


@app.command()
def main(
    domain: str = typer.Argument(..., help="Domain to look up; URLs, 'www.' prefixes, subdomains and paths are reduced to the root domain."),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output the full response as JSON."),
    as_csv: bool = typer.Option(False, "--csv", help="Output as CSV (header: subdomain)."),
    count_only: bool = typer.Option(False, "--count-only", "-c", help="Output only the number of subdomains found."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write to a file instead of stdout; a .txt/.json/.csv extension selects the format."),
    timeout: float = typer.Option(30.0, "--timeout", help="Request timeout in seconds.", min=1.0),
    retries: int = typer.Option(3, "--retries", help="Retries after HTTP 429/503.", min=0),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages on stderr."),
    version: bool = typer.Option(False, "--version", help="Show the version and exit.", callback=version_callback, is_eager=True),
) -> None:
    """Find the subdomains of DOMAIN using the free Subdomain API (subdomain.app)."""
    try:
        root = normalize_domain(domain)
        fmt = pick_format(as_json, as_csv, output)
        result = run_query(root, timeout=timeout, retries=retries, quiet=quiet)
        emit(result, fmt=fmt, count_only=count_only, output=output, quiet=quiet)
    except sdaError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    except BrokenPipeError:
        # stdout closed early (e.g. piped into 'head') - exit quietly
        raise typer.Exit() from None
    except OSError as exc:
        err_console.print(f"[red]error:[/red] could not write output: {exc}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        err_console.print("\n[yellow]interrupted[/yellow]")
        raise typer.Exit(code=130) from None


if __name__ == "__main__":
    app()