#!/usr/bin/env python3
"""Standalone CourtListener V4 opinion downloader (no permanent app integration).

Searches for likely police civil-lawsuit opinions and saves full-text candidates.
A human should verify each lawsuit's parties, subject, citations, and precedent status.
"""
from __future__ import annotations

import getpass
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = 'https://www.courtlistener.com'
SEARCH = BASE + '/api/rest/v4/search/'
OPINION = BASE + '/api/rest/v4/opinions/{}/'
OUTPUT = ROOT / 'AnythingLLM_Police_Civil_Cases'
CHECKPOINT = ROOT / 'download_progress.json'
TIMING_STATE = ROOT / 'request_timing.json'  # contains only a timestamp, never a token
MANIFEST = OUTPUT / 'manifest.jsonl'

# Narrow searches, rather than a giant OR query; results are filtered again locally.
QUERIES = [
    '"police department" "1983"',
    '"sheriff" "section 1983"',
    '"deputy sheriff" "false arrest"',
    '"police officer" "qualified immunity"',
    '"police" "excessive force"',
    '"police" "Title II" "ADA"',
    '"police department" "failure to investigate"',
    '"city" "police" "Monell"',
    '"sheriff" "sovereign immunity"',
    '"police" "retaliation" "First Amendment"',
    '"police department" "negligence"',
    '"police officer" "unlawful search"',
    '"Florida" "police" "civil rights"',
]

TARGET = 10000
try:
    TARGET = min(10000, max(1, int(os.getenv('CASE_COUNT', '10000'))))
except ValueError:
    raise SystemExit('CASE_COUNT must be a whole number between 1 and 10000')

# Make an authenticated request no more often than once every 15 seconds.
REQUEST_DELAY_SECONDS = 15
# Optional per-run cap. 0 means the script keeps going until stopped or finished.
try:
    REQUEST_BUDGET = int(os.getenv('MAX_REQUESTS_THIS_RUN', '0'))
    if REQUEST_BUDGET < 0:
        raise ValueError()
except ValueError:
    raise SystemExit('MAX_REQUESTS_THIS_RUN must be 0 (unlimited) or a positive whole number')
REQUEST_COUNT = 0

class HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.words = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.skip += 1
        if tag in ('p', 'div', 'br', 'li', 'blockquote', 'h1', 'h2', 'h3'):
            self.words.append('\n')
    def handle_endtag(self, tag):
        if tag in ('style', 'script') and self.skip:
            self.skip -= 1
        if tag in ('p', 'div', 'li', 'blockquote'):
            self.words.append('\n')
    def handle_data(self, data):
        if not self.skip:
            self.words.append(data)

def strip_html(value: str) -> str:
    parser = HTMLToText()
    parser.feed(value or '')
    result = html.unescape(''.join(parser.words))
    result = re.sub(r'\r\n?', '\n', result)
    result = re.sub(r'[ \t]+', ' ', result)
    return re.sub(r'\n{3,}', '\n\n', result).strip()

class LocalRunCap(Exception):
    pass

class APIError(Exception):
    pass


def trusted_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and parsed.netloc == 'www.courtlistener.com'
            and parsed.path.startswith('/api/rest/v4/'))


def wait_with_status(seconds: float, reason: str):
    """Wait in interruptible chunks and keep the terminal informative."""
    seconds = max(0, float(seconds))
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    deadline = dt.datetime.now().astimezone() + dt.timedelta(seconds=seconds)
    print(f'\n{reason}: waiting {seconds / 60:.1f} minutes; resuming around {deadline:%Y-%m-%d %I:%M:%S %p %Z}.', flush=True)
    # Print periodically while waiting, especially across a daily quota reset.
    interval = 600 if seconds >= 3600 else 60
    next_status = time.monotonic() + interval
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(10, remaining))
        if time.monotonic() >= next_status:
            remaining = max(0, end - time.monotonic())
            print(f'  Waiting for CourtListener allowance: {remaining / 60:.1f} minutes remaining. Ctrl+C to stop.', flush=True)
            next_status = time.monotonic() + interval


def load_timing_state() -> dict:
    """Read timing data, including any active server rate-limit deadline.

    The file contains timestamps only; never API credentials.
    """
    try:
        value = json.loads(TIMING_STATE.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def update_timing_state(**changes):
    state = load_timing_state()
    state.update(changes)
    atomic_json(TIMING_STATE, state)


def wait_before_request():
    state = load_timing_state()
    try:
        next_paced = float(state.get('last_attempt', 0)) + REQUEST_DELAY_SECONDS
        next_allowed = float(state.get('blocked_until', 0))
    except (ValueError, TypeError):
        next_paced, next_allowed = 0.0, 0.0
    remaining = max(next_paced, next_allowed) - time.time()
    if remaining > 0:
        reason = ('CourtListener rate limit' if next_allowed > next_paced
                  else '15-second request pacing')
        wait_with_status(remaining, reason)


def retry_after_seconds(headers) -> float | None:
    value = headers.get('Retry-After') if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def usage_api_reset_seconds(token: str) -> float | None:
    """Independent API-usage quota; called only when 429 lacks Retry-After."""
    url = BASE + '/api/rest/v4/api-usage/'
    req = urllib.request.Request(url, headers={
        'Authorization': 'Token ' + token,
        'Accept': 'application/json',
        'User-Agent': 'StandalonePoliceCivilCaseStudyDownloader/3.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.load(response)
        blocked = [r for r in data.get('current_usage', [])
                   if r.get('scope') == 'user' and r.get('remaining') == 0]
        if any(r.get('blocked') for r in blocked):
            raise APIError('CourtListener reports this token/account is blocked. Waiting will not fix it.')
        reset_times = []
        for row in blocked:
            raw = row.get('reset_at')
            if not raw:
                continue
            when = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            reset_times.append((when - dt.datetime.now(dt.timezone.utc)).total_seconds())
        return max(0.0, max(reset_times)) if reset_times else None
    except APIError:
        raise
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return None


def api_get(url: str, token: str) -> dict:
    """15-second pacing; retry 429 only after the server-specified reset."""
    global REQUEST_COUNT
    if not trusted_url(url):
        raise APIError('Untrusted API URL; refusing to send credentials')
    failures = 0
    while True:
        if REQUEST_BUDGET and REQUEST_COUNT >= REQUEST_BUDGET:
            raise LocalRunCap(f'Optional per-run request cap reached ({REQUEST_BUDGET}).')
        wait_before_request()
        req = urllib.request.Request(url, headers={
            'Authorization': 'Token ' + token,
            'Accept': 'application/json',
            'User-Agent': 'StandalonePoliceCivilCaseStudyDownloader/3.0 (research)',
        })
        # Persist timestamp before each request so restarting does not defeat pacing.
        update_timing_state(last_attempt=time.time())
        REQUEST_COUNT += 1
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                data = json.load(response)
                if not isinstance(data, dict):
                    raise APIError('Unexpected CourtListener response')
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                delay = retry_after_seconds(exc.headers)
                if delay is None:
                    delay = usage_api_reset_seconds(token)
                if delay is None:
                    # Conservative fallback if server supplies no usable reset info.
                    delay = 3600
                    print('No reset time was supplied; using a conservative 60-minute retry interval.')
                print('CourtListener HTTP 429. Your API token is still valid; do not reset it.')
                # Store the server-defined reset time so closing and reopening the
                # downloader cannot accidentally retry before the allowance resets.
                blocked_until = time.time() + max(delay + 5, REQUEST_DELAY_SECONDS)
                update_timing_state(blocked_until=blocked_until)
                print('Rate limit reset time saved; automatically waiting and retrying.')
                # Retry the SAME URL; wait_before_request() enforces blocked_until.
                continue
            if exc.code in (401, 403):
                raise APIError(f'HTTP {exc.code}: token invalid, revoked, or access forbidden. Check your token.') from None
            if 500 <= exc.code < 600 and failures < 5:
                failures += 1
                wait_with_status(max(REQUEST_DELAY_SECONDS, 60 * failures), f'CourtListener server error HTTP {exc.code}')
                continue
            raise APIError(f'HTTP {exc.code}: {exc.reason}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if failures >= 5:
                raise APIError(f'Network error after repeated retries: {exc}') from None
            failures += 1
            wait_with_status(max(REQUEST_DELAY_SECONDS, 60 * failures), 'Temporary network error')

def atomic_json(path: Path, obj: dict):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, indent=2), encoding='utf-8')
    tmp.replace(path)

def load_progress() -> dict:
    if CHECKPOINT.exists():
        saved = json.loads(CHECKPOINT.read_text(encoding='utf-8'))
        if saved.get('version') == 2:
            return saved
    return {'version': 2, 'query_index': 0, 'page_url': None, 'offset': 0}

def existing_records() -> set[str]:
    ids = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding='utf-8').splitlines():
            try:
                rec = json.loads(line)
                ids.add(str(rec.get('cluster_id')))
            except json.JSONDecodeError:
                pass
    return ids

def slug(s: str) -> str:
    return re.sub('[^a-zA-Z0-9-]+', '_', s).strip('_')[:85] or 'case'

# Heuristic, not a substitute for manual review. Defendant named in caption is
# strongest evidence; city/county cases require police-focused text also.
AGENCY_TITLE = re.compile(r'police|sheriff|deputy|officer|marshal|trooper|constable|chief of police', re.I)
CITY_TITLE = re.compile(r'\b(city|county|town|municipality|parish)\b', re.I)
CIVIL_ISSUE = re.compile(r'\b(1983|civil rights|qualified immunity|excessive force|false arrest|unlawful arrest|wrongful arrest|fourth amendment|fourteenth amendment|monell|deliberate indifference|negligence|wrongful death|state tort|failure to (investigate|train|supervise)|discrimination|title ii|ada|retaliation|civil action|civil complaint)\b', re.I)
POLICE_CONTEXT = re.compile(r'police department|sheriff.s office|deputy sheriff|police officer|law enforcement officer|police misconduct|officer misconduct', re.I)
CRIMINAL_CAPTION = re.compile(r'^(united states|state of [\w ]+|commonwealth of [\w ]+|people of [\w ]+)\s+v\.?\s+', re.I)

def candidate(title: str, text: str) -> bool:
    if CRIMINAL_CAPTION.match(title.strip()):
        return False
    if not CIVIL_ISSUE.search(text):
        return False
    if AGENCY_TITLE.search(title):
        return True
    return bool(CITY_TITLE.search(title) and POLICE_CONTEXT.search(text))

def opinion_text(opinion: dict) -> str:
    for key in ('html_with_citations', 'plain_text', 'html', 'html_lawbox', 'html_columbia'):
        raw = opinion.get(key) or ''
        if not isinstance(raw, str):
            continue
        content = strip_html(raw) if key.startswith('html') else raw.strip()
        if len(content) >= 1000:
            return content
    return ''

def save_case(result: dict, bodies: list[tuple[int, str]], query: str):
    title = result.get('caseNameFull') or result.get('caseName') or 'Unnamed case'
    cluster = result.get('cluster_id')
    absolute_url = result.get('absolute_url') or ''
    source = urllib.parse.urljoin(BASE, absolute_url)
    citations = result.get('citation') or []
    if not isinstance(citations, list):
        citations = [str(citations)]
    court = result.get('court') or result.get('court_id') or ''
    date = result.get('dateFiled') or ''
    fname = f'{cluster}_{slug(title)}.txt'
    head = (
        f'Case: {title}\n'
        f'Court: {court}\n'
        f'Decision date: {date}\n'
        f'Citations: {"; ".join(map(str, citations))}\n'
        f'Source: {source}\n'
        f'CourtListener cluster ID: {cluster}\n'
        f'Search that found this candidate: {query}\n'
        'Status: Automatically filtered candidate; verify this is a civil lawsuit against law enforcement and check subsequent treatment.\n'
    )
    sections = [f'\n==== OPINION {id_} ====\n\n{text}\n' for id_, text in bodies]
    (OUTPUT / fname).write_text(head + '\n' + ''.join(sections), encoding='utf-8')
    manifest_rec = {
        'cluster_id': cluster, 'file': fname, 'title': title, 'court': court,
        'date': date, 'citations': citations, 'source': source, 'search_query': query,
        'opinion_ids': [id_ for id_, _ in bodies], 'reviewed': False,
    }
    with MANIFEST.open('a', encoding='utf-8') as f:
        f.write(json.dumps(manifest_rec, ensure_ascii=False) + '\n')

def main():
    print('CourtListener V4 standalone downloader — police civil-law opinions')
    print('Use your own CourtListener API token. Keep it private.')
    token = os.getenv('COURTLISTENER_API_TOKEN', '').strip()
    if not token:
        token = getpass.getpass('Paste your CourtListener API token (hidden, local only): ').strip()
    if not token:
        raise SystemExit('No token entered. Nothing downloaded.')
    OUTPUT.mkdir(exist_ok=True)
    progress = load_progress()
    seen = existing_records()
    print(f'Already downloaded: {len(seen)}. Target: {TARGET}.')
    print('Interval: 15 seconds between requests, including retries and restarts.')
    print('HTTP 429: automatically waits for the CourtListener reset and resumes.')
    print('Press Ctrl+C to stop safely. Existing files and progress are preserved.')
    if REQUEST_BUDGET:
        print(f'Optional per-run request cap: {REQUEST_BUDGET}')
    try:
        while len(seen) < TARGET and progress['query_index'] < len(QUERIES):
            qi = progress['query_index']
            query = QUERIES[qi]
            url = progress['page_url'] or SEARCH + '?' + urllib.parse.urlencode({
                'q': query, 'type': 'o', 'stat_Unpublished': 'on',
            })
            data = api_get(url, token)
            results = data.get('results') or []
            if not isinstance(results, list):
                raise APIError('Unexpected search results')
            offset = progress.get('offset', 0)
            for i in range(offset, len(results)):
                result = results[i]
                # checkpoint before each possible opinion fetch
                progress.update(page_url=url, offset=i)
                atomic_json(CHECKPOINT, progress)
                cid = result.get('cluster_id')
                if cid is None or str(cid) in seen:
                    progress['offset'] = i + 1
                    continue
                title = str(result.get('caseNameFull') or result.get('caseName') or '')
                if CRIMINAL_CAPTION.match(title):
                    progress['offset'] = i + 1
                    continue
                refs = result.get('opinions') or []
                ids = [o.get('id') for o in refs if isinstance(o, dict) and o.get('id') is not None]
                bodies = []
                for oid in ids:
                    op = api_get(OPINION.format(urllib.parse.quote(str(oid), safe='')), token)
                    body = opinion_text(op)
                    if body:
                        bodies.append((int(oid), body))
                combined = '\n\n'.join(body for _, body in bodies)
                if combined and candidate(title, combined):
                    save_case(result, bodies, query)
                    seen.add(str(cid))
                    print(f'{len(seen)}/{TARGET}: {title[:80]}')
                progress['offset'] = i + 1
                atomic_json(CHECKPOINT, progress)
                if len(seen) >= TARGET:
                    break
            if len(seen) >= TARGET:
                break
            next_url = data.get('next')
            if next_url:
                if not trusted_url(next_url):
                    raise APIError('Unexpected pagination host; refusing to follow.')
                progress.update(page_url=next_url, offset=0)
            else:
                progress.update(query_index=qi + 1, page_url=None, offset=0)
            atomic_json(CHECKPOINT, progress)
        if progress['query_index'] >= len(QUERIES) and len(seen) < TARGET:
            print('Searches exhausted; fewer than target candidates qualified. Add more targeted queries as needed.')
    except (LocalRunCap, APIError) as exc:
        atomic_json(CHECKPOINT, progress)
        print('\nPaused safely:', exc)
    finally:
        print(f'\nSaved {len(seen)} case files to: {OUTPUT}')
        print(f'API requests this run: {REQUEST_COUNT}')
        print('Session finished or paused for a non-rate-limit error. Window stays open for review.')

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped by user. Existing files are preserved.')
