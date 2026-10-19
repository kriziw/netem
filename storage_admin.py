"""Administrator storage visibility and bounded, lossless telemetry maintenance."""
import copy
import hmac
import os
from pathlib import Path
import sqlite3
import threading
import time

from flask import abort, g, jsonify, render_template, request, session

WARNING_PERCENT = 80.0
COMPACTION_MARGIN = 64 * 1024 * 1024


def human_bytes(value):
    if value is None:
        return 'Unavailable'
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(amount) < 1024 or unit == 'TiB':
            return f'{amount:.1f} {unit}' if unit != 'B' else f'{int(amount)} B'
        amount /= 1024


def disk_usage(path):
    try:
        info = os.statvfs(path)
        total = info.f_blocks * info.f_frsize
        used = (info.f_blocks - info.f_bfree) * info.f_frsize
        available = info.f_bavail * info.f_frsize
        # Match df: filesystem-reserved blocks are not available to the service.
        percent = 100.0 * used / (used + available) if used + available else None
        return dict(total=total, used=used, available=available,
                    used_percent=round(percent, 1) if percent is not None else None,
                    warning=percent is not None and percent >= WARNING_PERCENT, error=None)
    except OSError:
        return dict(total=None, used=None, available=None, used_percent=None, warning=False,
                    error='Disk capacity could not be read.')


def file_size(path):
    try:
        if path.is_symlink():
            return None
        return path.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def journal_size(roots=(Path('/var/log/journal'), Path('/run/log/journal'))):
    """Read allocation without spawning journalctl; never traverse links or scan without a bound."""
    total, count, errors = 0, 0, []
    deadline = time.monotonic() + 1
    def failed(_error):
        errors.append(True)
    for root in roots:
        if root.is_symlink():
            errors.append(True)
            continue
        try:
            if not root.exists():
                continue
        except OSError:
            errors.append(True)
            continue
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
            for name in files:
                count += 1
                if count > 20000 or time.monotonic() > deadline:
                    return dict(bytes=None, error='Journal size scan exceeded its limit.')
                path = Path(folder) / name
                try:
                    if not path.is_symlink():
                        total += path.stat().st_blocks * 512
                except OSError:
                    errors.append(True)
    return dict(bytes=None if errors else total,
                error='Journal size is unavailable with the service permissions.' if errors else None)


class StorageAdmin:
    def __init__(self, netem):
        self.netem = netem
        self.lock = threading.Lock()
        self.job = dict(state='idle', message=None, started_at=None, finished_at=None,
                        before_bytes=None, after_bytes=None, reclaimed_bytes=None)

    def status(self):
        with self.lock:
            return copy.deepcopy(self.job)

    def running(self):
        return self.status()['state'] == 'running'

    def snapshot(self):
        n = self.netem
        databases = []
        for label, path in (('Telemetry', n.TELEMETRY_DB_PATH), ('Users & access', n.ACCESS_STORE.path),
                            ('Appliance inventory', n.APPLIANCE_PLATFORM.store.path)):
            main, wal, shm = [file_size(Path(str(path) + suffix)) for suffix in ('', '-wal', '-shm')]
            databases.append(dict(name=label, main_bytes=main, wal_bytes=wal, shm_bytes=shm,
                                  total_bytes=main + wal + shm if None not in (main, wal, shm) else None))
        return dict(checked_at=time.time(), disk=disk_usage(n.RUNTIME_DIR), databases=databases,
                    event_log_bytes=file_size(n.EVENT_LOG_PATH), journal=journal_size(), job=self.status())

    def idle(self):
        n = self.netem
        with n.RUNTIME_LOCK:
            return not (n.ACTIVE_SESSION.get('active') or n.SCENARIO_STATE.get('active')
                        or n.SITE_PLAN_STATE.get('active') or n.CAPTURE_STATE.get('active')
                        or n.TEST_RECORDER.get('active') or n.EPHEMERAL_TELEMETRY is not None)

    def start(self):
        with self.lock:
            if self.job['state'] == 'running':
                raise ValueError('Database compaction is already running.')
            self.job = dict(state='running', message='Compacting telemetry database…', started_at=time.time(),
                            finished_at=None, before_bytes=None, after_bytes=None, reclaimed_bytes=None)
        try:
            threading.Thread(target=self.compact, name='netem-database-compaction', daemon=True).start()
        except Exception:
            with self.lock:
                self.job.update(state='failed', message='Database compaction could not start.', finished_at=time.time())
            raise

    def compact(self):
        conn = None
        try:
            path = self.netem.TELEMETRY_DB_PATH
            if path.is_symlink() or not path.is_file():
                raise ValueError('Telemetry database is unavailable.')
            # Compaction preserves rows. Never replace, recreate or delete a database/sidecar.
            before = file_size(path)
            self.require_space(path, before)
            conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=1, isolation_level=None)
            deadline = time.monotonic() + 120
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Database integrity check failed. Compaction was not performed.')
            if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise ValueError('Database is busy. Wait for other database operations and retry.')
            self.require_space(path, file_size(path))
            conn.execute('VACUUM')
            if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise ValueError('Compaction finished but its WAL checkpoint is busy. Retry when the database is idle.')
            after = file_size(path)
            with self.lock:
                self.job.update(state='completed', message='Telemetry database compacted. All records preserved.',
                                before_bytes=before, after_bytes=after, reclaimed_bytes=max(0, before - after))
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.netem.app.logger.exception('Telemetry database compaction failed')
            message = str(exc) if isinstance(exc, ValueError) else 'Database compaction failed. Check server logs and storage availability.'
            with self.lock:
                self.job.update(state='failed', message=message)
        except Exception:
            self.netem.app.logger.exception('Unexpected telemetry compaction error')
            with self.lock:
                self.job.update(state='failed', message='Database compaction failed. Check server logs.')
        finally:
            if conn is not None:
                conn.close()
            with self.lock:
                self.job['finished_at'] = time.time()

    @staticmethod
    def require_space(path, size):
        if size is None:
            raise ValueError('Database size is unavailable; compaction cannot safely start.')
        required = 2 * size + COMPACTION_MARGIN
        available = disk_usage(path.parent)['available']
        # SQLite's Unix VFS searches these locations in this documented order,
        # which differs from Python's tempfile selection (notably /var/tmp).
        candidates = [value for value in (os.environ.get('SQLITE_TMPDIR'), os.environ.get('TMPDIR'),
                      '/var/tmp', '/usr/tmp', '/tmp', str(Path.cwd())) if value]
        temp_path = next((Path(value) for value in candidates
                          if Path(value).is_dir() and os.access(value, os.W_OK | os.X_OK)), None)
        temp_available = disk_usage(temp_path)['available'] if temp_path else None
        if available is None or temp_available is None or available < required or temp_available < size + COMPACTION_MARGIN:
            raise ValueError('Not enough working space to compact safely. Expand storage first; allow twice the database size plus 64 MiB on its disk, and the database size plus 64 MiB for temporary files.')


def init_app(app, netem):
    storage = StorageAdmin(netem)
    app.jinja_env.filters['storage_bytes'] = human_bytes
    allowed = app.extensions['netem_access_allowed']

    @app.context_processor
    def admin_storage_context():
        return {'admin_storage_disk': disk_usage(netem.RUNTIME_DIR) if allowed('storage_settings') else None}

    # Serialize admission with the existing integration start lock. The lock is held through
    # the request, so a test/session and a compaction cannot both pass their idle checks.
    guarded = {'storage_compact', 'session_start', 'lab_scenario_start', 'site_run', 'traffic_generator_start', 'lab_capture_start'}
    @app.before_request
    def storage_admission():
        if request.method != 'POST' or request.endpoint not in guarded:
            return
        if request.endpoint == 'storage_compact':
            supplied, expected = request.form.get('integration_csrf', ''), session.get('integration_csrf', '')
            if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                abort(400, 'Invalid form token. Reload the page and retry.')
        netem.APPLIANCE_PLATFORM.operations.acquire()
        g.storage_admission = True
        if request.endpoint != 'storage_compact' and storage.running():
            abort(409, 'Database maintenance is running. Wait for compaction to finish before starting lab operations.')

    app.before_request_funcs[None].insert(1, app.before_request_funcs[None].pop())

    @app.teardown_request
    def release_storage_admission(_error):
        if getattr(g, 'storage_admission', False):
            g.storage_admission = False
            netem.APPLIANCE_PLATFORM.operations.release()

    def snapshot():
        result = netem.PRESENTATION_READS.read('admin-storage', str(netem.RUNTIME_DIR), storage.snapshot,
            {'checked_at': None, 'databases': [], 'journal': {'bytes': None}, 'event_log_bytes': None},
            max_age=10, max_stale=30)
        result.update(disk=disk_usage(netem.RUNTIME_DIR), job=storage.status())
        return result

    @app.get('/settings/storage')
    def storage_settings():
        return render_template('storage.html', page='settings', storage=snapshot())

    @app.get('/api/v1/storage')
    def storage_status():
        return jsonify(snapshot())

    @app.post('/settings/storage/compact')
    def storage_compact():
        if not storage.idle():
            return jsonify(error='Finish the active session, test, plan or capture before compacting the database.'), 409
        if netem.traffic_generator_config().get('host'):
            try:
                remote = netem.traffic_generator_request('/api/v1/status', timeout=4)
            except RuntimeError:
                return jsonify(error='Simulator status could not be verified. Stop its workload and restore connectivity before compaction.'), 409
            if remote.get('status') not in ('idle', 'stopped', 'failed', 'interrupted'):
                return jsonify(error='Stop the simulator workload before compacting the database.'), 409
        if not storage.idle():
            return jsonify(error='The lab started another operation. Stop it before compacting the database.'), 409
        try:
            storage.require_space(netem.TELEMETRY_DB_PATH, file_size(netem.TELEMETRY_DB_PATH))
            storage.start()
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(accepted=True, job=storage.status()), 202

    return storage
