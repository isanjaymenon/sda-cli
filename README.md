# sdf - Subdomain Finder CLI

Find the subdomains of any domain from the terminal, powered by the free
[Subdomain API](https://subdomain.app) - no signup, no API key.

Single-file Python script with inline dependency metadata (PEP 723), run with
[uv](https://docs.astral.sh/uv/). No install, no venv management.

## Quick start

```powershell
uv run sdf.py example.com
```

The first run resolves `httpx`, `rich`, and `typer` into uv's cache; every run
after that starts instantly.

## Usage

```
uv run sdf.py <domain> [options]
```

The input is forgiving - these all look up `example.com`:

```powershell
uv run sdf.py example.com
uv run sdf.py https://www.example.com/about
uv run sdf.py blog.example.com
uv run sdf.py "user:pass@example.com:8080/path"
```

### Options

| Option | Description |
| --- | --- |
| `--json`, `-j` | Output the full response as JSON (`domain`, `count`, `total`, `subdomains`). |
| `--csv` | Output as CSV with a `subdomain` header. |
| `--count-only`, `-c` | Output only the number of subdomains returned. |
| `--output`, `-o FILE` | Write to a file instead of stdout; a `.txt` / `.json` / `.csv` extension selects the format. |
| `--timeout SECONDS` | Request timeout (default: 30). |
| `--retries N` | Retries after HTTP 429/503 with exponential backoff, honoring `Retry-After` (default: 3). |
| `--quiet`, `-q` | Suppress status messages on stderr. |
| `--version` | Show the version and exit. |
| `--help` | Show help. |

### Examples

```powershell
# Pipe-friendly: plain list, one subdomain per line
uv run sdf.py github.com --quiet | sort

# Just the count
uv run sdf.py github.com --count-only

# JSON to a file (format inferred from extension)
uv run sdf.py github.com -o github-subs.json

# CSV via flag
uv run sdf.py github.com --csv -o github-subs.csv
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (including "no subdomains found"). |
| 1 | API or network error (invalid domain rejected by the API, exhausted retries, timeout). |
| 2 | Usage error (invalid input, conflicting flags). |
| 130 | Interrupted (Ctrl+C). |

## How it works

- Queries `GET https://api.subdomain.app/v1/query?domain=<domain>`.
- Input is normalized locally (scheme, `www.`, `*.`, credentials, port, path
  stripped), then the API further reduces it to the registrable domain.
- Results are printed one per line on **stdout**; the spinner, summary panel,
  and warnings go to **stderr**, so piping stays clean.
- Up to 10,000 subdomains per domain (most recently seen first). When the
  index knows more (`total` > `count`), the summary panel says so.
- HTTP 429/503 responses are retried with exponential backoff + jitter,
  honoring the `Retry-After` header.

## Data caveat

Results come from a passive, historic index. Some subdomains may no longer
resolve, others may be brand new, and coverage is best-effort - verify before
relying on the data. Only queried domains are stored by the service; see the
site's privacy policy and terms.

## Development

Run the offline test suite (mocked HTTP via `httpx.MockTransport`, CLI via
typer's `CliRunner`):

```powershell
uv run test_sdf.py
```

`test_sdf.py` carries its own PEP 723 header including pytest, so no project
setup is needed.