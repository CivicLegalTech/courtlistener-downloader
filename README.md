# CourtListener Civil Case Downloader

A standalone Python script that searches CourtListener for likely civil lawsuits involving police and saves available opinion text and source metadata locally.

## Features

- Thirteen targeted searches covering civil rights, qualified immunity, excessive force, ADA, negligence, and related issues.
- Fifteen-second pacing between ordinary requests, including retries and restarts.
- Automatic waiting and retrying after HTTP 429 responses, using server reset information when available; otherwise a conservative one-hour fallback.
- Progress checkpoints and duplicate avoidance using a local manifest.
- Plain-text opinion files with court, date, citations, and source links.
- Python standard library only; no pip packages required.

## Quick start

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
2. Extract the release ZIP into a writable folder.
3. Obtain your own [CourtListener API token](https://www.courtlistener.com/profile/api-token/).
4. On Windows, double-click `START_WINDOWS.bat`. On other systems, run `python3 download.py` in a terminal.
5. Paste your token at the prompt. Input stays hidden, hit enter.

Downloaded cases appear in `AnythingLLM_Police_Civil_Cases/`. You can import the `.txt` files into a document or retrieval tool manually. The script has no direct AnythingLLM integration.

Press **Ctrl+C** to stop. Run it again to resume. Keep the output folder, `download_progress.json`, and `request_timing.json` together with the script. Run one instance per folder. Do not delete timing state to bypass a cooldown.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `CASE_COUNT` | `10000` | Target number of saved candidates, clamped to 1–10,000. |
| `MAX_REQUESTS_THIS_RUN` | `0` | Optional request cap; zero means no local cap. Account limits still apply. |
| `COURTLISTENER_API_TOKEN` | unset | Optional alternative to the hidden token prompt. |

Example in Windows Command Prompt:

```bat
set CASE_COUNT=100
set MAX_REQUESTS_THIS_RUN=70
START_WINDOWS.bat
```

Example in PowerShell:

```powershell
$env:CASE_COUNT = '100'
$env:MAX_REQUESTS_THIS_RUN = '70'
py -3 download.py
```

The default run continues until it reaches its target, exhausts its searches, encounters a stopping error, or you stop it. Long account cooldowns may leave the process waiting for hours. The optional cap is a local safeguard, not a measure of remaining account quota; a quota-usage lookup can make an additional request.

## Output and limitations

Each saved `.txt` file contains metadata and available opinion text. `manifest.jsonl` records saved cluster IDs and source metadata. Only opinions with at least 1,000 characters of usable text are included. PDF-only opinions are not downloaded or OCRed.

Filtering uses case captions and text patterns. Results can contain false positives and miss relevant cases. This tool does not classify wins and losses, verify subsequent treatment, or determine whether a decision is binding or still good law. The target is not a guarantee that 10,000 qualifying cases exist.

Searches include unpublished opinions. Review the full source and applicable citation rules before relying on any result. The output files are candidates for human review, not legal advice.

## Credentials and local data

Use your own token and follow your account's API terms and limits. The script does not intentionally write tokens to disk. Never commit tokens, environment files, screenshots containing credentials, or local logs containing secrets. Reset a token if it is exposed.

Release archives contain source and documentation only. Downloaded opinions, manifests, timing state, and checkpoints are excluded.

## Development

Run the offline checks without using a token or API allowance:

```sh
python -m unittest discover -s tests -v
```

## Project status

Initial source release candidate: **v4.0.0**. This is an independent project and is not an official CourtListener or Free Law Project product.

The software is released under the [MIT License](LICENSE). Downloaded court materials retain their own applicable rights and terms.

API documentation: [Search](https://wiki.free.law/c/courtlistener/help/api/rest/v4/search) · [Case law](https://wiki.free.law/c/courtlistener/help/api/rest/v4/case-law)
