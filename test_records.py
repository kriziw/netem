"""Durable per-test troubleshooting records, separate from the last-test summary."""
import json
import re
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.parse import urlencode, quote

_LOCK = threading.Lock()
MAX_JOURNAL_BYTES = 64 * 1024 * 1024


def scrub(value):
    if isinstance(value, dict):
        return {key: ("[redacted]" if re.search(r"password|secret|token|api.?key|authorization|cookie|private.?key", str(key), re.I)
                      else scrub(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        def clean_url(match):
            try:
                url = urlsplit(match.group())
                host = url.netloc.rsplit('@', 1)[-1]
                return urlunsplit((url.scheme, host, url.path, "[redacted]" if url.query else "", ""))
            except ValueError:
                return "[redacted URL]"
        value = re.sub(r"https?://[^\s\"'<>]+", clean_url, value)
        return re.sub(r"(?i)(bearer\s+|(?:password|token|api[_-]?key)=)[^\s&,;]+", r"\1[redacted]", value)
    return value


def directory(root, test_id):
    if not re.fullmatch(r"[0-9a-f]{32}", test_id or ""):
        raise ValueError("Invalid test record ID")
    return Path(root) / 'test-records' / test_id


def write(root, test_id, name, value):
    target = directory(root, test_id)
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(scrub(value), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def append(root, test_id, kind, value):
    path = directory(root, test_id) / 'timeline.jsonl'
    encoded = json.dumps(scrub(dict(kind=kind, data=value)), ensure_ascii=False) + '\n'
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_JOURNAL_BYTES:
            marker = path.parent / 'journal-truncated.json'
            if not marker.exists():
                write(root, test_id, marker.name, {'complete': False, 'reason': '64 MiB journal resource limit reached'})
            return
        with path.open('a', encoding='utf-8') as handle:
            handle.write(encoded)


def archive(root, test_id):
    folder = directory(root, test_id)
    if not (folder / 'configuration.json').is_file():
        raise FileNotFoundError(test_id)
    target = folder / 'record.zip'
    temporary = folder / 'record.zip.tmp'
    with _LOCK:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(folder.iterdir()):
                if path.suffix in ('.json', '.jsonl', '.txt'):
                    bundle.write(path, path.name)
        temporary.replace(target)
    return target


def history(root):
    records = []
    for path in (Path(root) / 'test-records').glob('*/configuration.json'):
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
            summary_path = path.parent / 'summary.json'
            summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
            records.append({'test_id': path.parent.name, 'name': config.get('scenario', {}).get('name', 'Test'),
                            'started_at': config.get('started_at'), 'result': summary.get('result', 'unfinished')})
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda item: item.get('started_at') or 0, reverse=True)


def collect_agent(root, test_id, run_ids, since, until, fetch):
    """Archive pages immediately; never silently treat a partial export as complete."""
    status = {'complete': True, 'runs': {}, 'errors': [], 'limits': '100 pages of 1000 rows per run and evidence kind'}
    deadline = time.monotonic() + 30
    if not run_ids:
        status.update(complete=False, errors=['No simulator run identity was recorded'])
    for run_id in run_ids:
        run_status = status['runs'][run_id] = {}
        for kind in ('transactions', 'operations', 'contracts'):
            cursor, ceiling = 0, None
            detail = run_status[kind] = {'complete': False, 'rows': 0}
            try:
                for page in range(100):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('30 second total export time budget reached')
                    query = dict(since=since, until=until, kind=kind, cursor=cursor, limit=1000)
                    if ceiling is not None:
                        query['ceiling'] = ceiling
                    data = fetch(f'/api/v1/runs/{quote(run_id, safe="")}/evidence?{urlencode(query)}', timeout=min(7.0, max(.1, deadline-time.monotonic())))
                    if data.get('run_id') != run_id or data.get('kind') != kind or not isinstance(data.get('items'), list):
                        raise ValueError('Invalid simulator evidence response')
                    # File names use a local ordinal, never a remote run identifier.
                    prefix = list(status['runs']).index(run_id)
                    write(root, test_id, f'agent-{prefix}-{kind}-{page:03}.json', data)
                    detail['rows'] += len(data['items'])
                    ceiling = data['ceiling']
                    next_cursor = data.get('next_cursor')
                    if next_cursor is None:
                        detail['complete'] = bool(data.get('complete'))
                        break
                    if not isinstance(next_cursor, int) or next_cursor <= cursor:
                        raise ValueError('Simulator evidence pagination did not advance')
                    cursor = next_cursor
                if not detail['complete']:
                    status['complete'] = False
                    status['errors'].append(f'{run_id}/{kind}: export resource limit or incomplete page')
            except (RuntimeError, ValueError, KeyError, OSError) as exc:
                status['complete'] = False
                status['errors'].append(f'{run_id}/{kind}: {exc}')
    write(root, test_id, 'agent-export-status.json', status)
    return status
