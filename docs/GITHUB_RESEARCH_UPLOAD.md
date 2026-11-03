# Automatic private GitHub research uploads

TradingGPT can automatically publish its finalized end-of-day research package to a **separate private GitHub repository**. It never uploads research artifacts to the source-code repository.

## Why a separate private repository is required

The source repository 'siddumoni/tradinggpt' is public. The uploader checks the destination repository metadata and refuses to write unless GitHub confirms 'private: true'. Do not point the uploader at the public source repository.

Create a new private repository initialized with a README (so the default branch exists), for example:

- Repository: 'siddumoni/tradinggpt-research-data'
- Visibility: **Private**
- Default branch: 'main'

The daily files are Kite-derived market-data archives and operational research logs. Keep the repository private and limit access to people who need it.

## Create a least-privilege token

Create a GitHub fine-grained personal access token with:

- Resource owner: the account that owns the private research repository.
- Repository access: only 'tradinggpt-research-data'.
- Repository permissions: **Contents — Read and write**.
- Metadata read access is required by GitHub for repository inspection.

Do not use a classic token with broad access when a fine-grained token is available. Never commit the token to this repository, put it in a script/command argument, or paste it into the service log.

## Configure Windows

Open **Edit environment variables for your account** in Windows. Add these User variables:

| Variable | Example value |
|---|---|
| 'TRADINGGPT_RESEARCH_REPO' | 'siddumoni/tradinggpt-research-data' |
| 'TRADINGGPT_RESEARCH_TOKEN' | The fine-grained token you created |
| 'TRADINGGPT_RESEARCH_BRANCH' | 'main' (optional; the repo default branch is used when omitted) |

Close existing PowerShell windows and start a new one after changing User variables. The paper service reads environment variables when it starts.

Do not put these values into '.env' or source code. The uploader reads them directly from the process environment and never logs the token.

## What happens at end of day

At the 15:20 IST report finalization, or during Ctrl+C graceful shutdown:

1. Refresh the Challenger research book.
2. Generate the daily Markdown, JSON and combined trade CSV reports.
3. Persist the final daily-state JSON.
4. Write the upload checklist and structured artifact inventory.
5. Verify that all REQUIRED files exist.
6. Verify the configured GitHub destination is private.
7. Commit the available daily artifacts in one Git commit.

A failed or incomplete report skips the upload. Authentication, network, privacy-check, file-validation or GitHub errors are logged and do **not** stop the paper service or its shutdown.

Repeated finalization on the same day is safe: files with unchanged Git blob hashes are skipped. A later graceful shutdown can update files (for example, the service log) that changed after the first end-of-day snapshot.

## Folder layout

Each report date gets its own folder:

~~~text
daily/
└── 2026-11-03/
    ├── manifest.json
    ├── reports/
    │   ├── paper_daily_2026-11-03.md
    │   ├── paper_daily_2026-11-03.json
    │   ├── paper_daily_2026-11-03_trades.csv
    │   └── paper_upload_checklist_2026-11-03.txt
    ├── scans/
    │   └── phase6_scan_20261103_*.json
    ├── diagnostics/
    │   ├── paper_daily_report_state.json
    │   ├── paper_scan_runs.csv
    │   ├── paper_signals.csv
    │   └── paper_scan_scheduler_state.json
    ├── logs/
    │   └── paper_trading_service.log
    └── candles/
        ├── RELIANCE.csv
        ├── HDFCBANK.csv
        └── ...
~~~

The exact stock archives depend on the screened/open symbols and candle files updated on that IST date.

The SQLite paper-account database is **not uploaded**. '.env', credential/token files, browser profiles, and files outside the project root are also excluded. A credential-pattern scan is performed over each candidate text artifact before any GitHub writes; if a suspicious token pattern is found, the whole upload is cancelled. Files above 90 MiB are not sent through the GitHub API; an oversized required artifact fails the batch rather than silently publishing an incomplete required package.

The uploader uses GitHub's Git Database API to add all changed files and the manifest in a single commit under 'daily/YYYY-MM-DD/'. It performs no action on the Kite account and has no order-placement path.

## Confirm successful upload

After end-of-day finalization, inspect 'logs/paper_trading_service.log' for one of these messages:

- 'Private GitHub daily research upload completed': upload committed or all files already current.
- 'GitHub research upload is not configured': set the environment variables above.
- 'Refusing upload ... not a private repository': the configured destination is public; use a private repository.
- 'Private GitHub daily research upload failed': inspect the reason (permissions, branch, network or data validation).

The daily folder's 'manifest.json' contains the report date, repository, generation timestamp, uploaded filenames, sizes and SHA-256 hashes. It does not contain the GitHub token.

## Manual verification

In a fresh PowerShell session, verify that the variables exist without printing the token:

~~~powershell
[bool]$env:TRADINGGPT_RESEARCH_REPO
[bool]$env:TRADINGGPT_RESEARCH_TOKEN
$env:TRADINGGPT_RESEARCH_REPO
~~~

Then run the integrated service normally:

~~~powershell
python -m src.paper_trading_service
~~~

The actual upload occurs at daily report finalization, not on every five-minute scan.
