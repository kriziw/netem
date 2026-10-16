"""Local identities, Authentik OIDC and server-enforced operator permissions.

Provision recovery access: python access_control.py create-admin USERNAME
The first local administrator enables authentication for the operator listener.
"""
import argparse
from contextlib import contextmanager
import getpass
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import abort, g, jsonify, redirect, render_template, request, session, url_for, flash
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ('viewer', 'presenter', 'operator', 'admin')
TEST_CONTROLS = {'session_start', 'session_stop', 'site_run', 'site_stop', 'lab_scenario_start',
                 'lab_scenario_pause', 'lab_scenario_resume', 'lab_scenario_stop', 'lab_scenario_phase'}
OPERATOR_CONTROLS = {'quick_wan_action', 'lab_fault', 'lab_mtu', 'lab_sla', 'clear',
    'configure', 'presets', 'reset_presets', 'probe_save', 'probe_delete', 'probe_run',
    'lab_scenario_save', 'lab_scenario_delete', 'lab_capture_start', 'lab_capture_stop',
    'traffic_generator_start', 'traffic_generator_adjust', 'traffic_generator_stop',
    'site_save', 'site_apply_wan', 'wan_egress'}
ADMIN_READS = {'integrations', 'settings', 'setup', 'updates', 'update_components', 'updates_status',
    'branding_settings', 'branding_preview', 'branding_export', 'traffic_settings', 'traffic_settings_data',
    'simulator_controller', 'access_settings'}
PUBLIC = {'static', 'access_login', 'access_oidc_login', 'access_oidc_callback'}


def required_role(endpoint, method='GET'):
    if endpoint in PUBLIC or endpoint == 'access_logout':
        return 'viewer'
    if endpoint in ADMIN_READS or (endpoint or '').startswith('access_'):
        return 'admin'
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return 'viewer'
    if endpoint in TEST_CONTROLS:
        return 'presenter'
    if endpoint in OPERATOR_CONTROLS:
        return 'operator'
    return 'admin'  # New mutation routes fail closed until explicitly classified.


class AccessStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with private permissions before SQLite opens the file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, '
                         'password TEXT, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, '
                         'version INTEGER NOT NULL DEFAULT 1, issuer TEXT, subject TEXT, UNIQUE(issuer, subject))')
            conn.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            with conn:
                yield conn
        finally:
            conn.close()

    def enabled(self):
        if not self.path.exists():
            return False
        with self.connect() as conn:
            return bool(conn.execute('SELECT 1 FROM users LIMIT 1').fetchone())

    def users(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT id,username,role,active,version,issuer,subject FROM users ORDER BY username')]

    def get(self, user_id):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            return dict(row) if row else None

    def create(self, username, password, role):
        username = str(username).strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.@-]{1,79}', username):
            raise ValueError('Username must contain 2–80 letters, digits or . _ @ - characters.')
        if role not in ROLES:
            raise ValueError('Choose a valid role.')
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('Use a password of 12–256 characters.')
        hashed = generate_password_hash(password)
        user_id = secrets.token_hex(16)
        with self.lock, self.connect() as conn:
            if not conn.execute('SELECT 1 FROM users LIMIT 1').fetchone() and role != 'admin':
                raise ValueError('The first account must be a local administrator.')
            try:
                conn.execute('INSERT INTO users(id,username,password,role) VALUES(?,?,?,?)',
                             (user_id, username, hashed, role))
            except sqlite3.IntegrityError as exc:
                raise ValueError('This username already exists.') from exc
        return user_id

    def update(self, user_id, role, active, password=''):
        if role not in ROLES:
            raise ValueError('Choose a valid role.')
        if password and not 12 <= len(password) <= 256:
            raise ValueError('Use a password of 12–256 characters.')
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise ValueError('User not found.')
            if row['issuer'] and password:
                raise ValueError('OIDC passwords are managed in Authentik.')
            if row['password'] and row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
                count = conn.execute("SELECT count(*) FROM users WHERE password IS NOT NULL AND role='admin' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError('Keep at least one enabled local administrator for recovery.')
            conn.execute('UPDATE users SET role=?,active=?,version=version+1 WHERE id=?', (role, int(active), user_id))
            if password:
                conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(password), user_id))

    def authenticate(self, username, password):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE username=?', (username.strip().lower(),)).fetchone()
        # Equal-cost dummy check prevents trivial account enumeration by timing.
        hashed = row['password'] if row and row['password'] else DUMMY_PASSWORD
        valid = check_password_hash(hashed, password)
        return dict(row) if row and row['active'] and row['password'] and valid else None

    def oidc(self):
        if not self.path.exists():
            return {}
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM settings WHERE id=1').fetchone()
            return json.loads(row[0]) if row else {}

    def save_oidc(self, settings):
        if settings.get('enabled'):
            import importlib.util
            if not importlib.util.find_spec('authlib') or not importlib.util.find_spec('requests'):
                raise ValueError('Install OIDC dependencies first: .venv/bin/python -m pip install -r requirements.txt')
            for key in ('issuer', 'redirect_uri'):
                parsed = urlsplit(settings.get(key, ''))
                if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    raise ValueError('OIDC issuer and callback must be HTTPS URLs without credentials, query or fragment.')
            if not settings.get('client_id') or not settings.get('client_secret'):
                raise ValueError('Enter the OIDC client ID and secret.')
        mappings = settings.get('groups')
        if not isinstance(mappings, dict) or any(not isinstance(k, str) or not k or v not in ROLES for k, v in mappings.items()):
            raise ValueError('Group mappings must be a JSON object mapping Authentik group names to roles.')
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO settings(id,value) VALUES(1,?)', (json.dumps(settings),))
            # Reconfiguration immediately invalidates existing SSO sessions.
            conn.execute('UPDATE users SET version=version+1 WHERE issuer IS NOT NULL')

    def oidc_user(self, claims, settings):
        issuer, subject = claims.get('iss'), claims.get('sub')
        if issuer != settings.get('issuer') or not isinstance(subject, str) or not subject:
            raise ValueError('OIDC identity issuer or subject is invalid.')
        groups = claims.get('groups', [])
        if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
            raise ValueError('OIDC groups claim must be an array of group names.')
        roles = [settings['groups'][group] for group in groups if group in settings.get('groups', {})]
        if not roles:
            raise ValueError('No NetEm role is mapped to this Authentik account.')
        role = max(roles, key=ROLES.index)
        with self.lock, self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
            if row and not row['active']:
                raise ValueError('This account has been disabled by a NetEm administrator.')
            if row:
                conn.execute('UPDATE users SET role=?,version=version+1 WHERE id=?', (role, row['id']))
                user_id = row['id']
            else:
                user_id = secrets.token_hex(16)
                # Names never link an external identity to an existing local account.
                name = str(claims.get('preferred_username') or claims.get('name') or 'Authentik user')[:80]
                conn.execute('INSERT INTO users(id,username,role,issuer,subject) VALUES(?,?,?,?,?)',
                             (user_id, f'{name} [OIDC {user_id[:8]}]', role, issuer, subject))
        return self.get(user_id)


DUMMY_PASSWORD = generate_password_hash(secrets.token_urlsafe(32))


def init_app(app, store):
    app.extensions['netem_access'] = store
    attempts, attempts_lock = {}, threading.Lock()
    app.config.setdefault('NETEM_AUTH_REQUIRED', os.environ.get('NETEM_AUTH_REQUIRED') == '1')
    app.config.setdefault('NETEM_SECURE_COOKIES', os.environ.get('NETEM_SECURE_COOKIES') == '1')
    app.config['SESSION_COOKIE_SECURE'] = app.config['NETEM_SECURE_COOKIES']

    def enabled():
        return app.config['NETEM_AUTH_REQUIRED'] or store.enabled()

    def csrf():
        token = request.form.get('integration_csrf') or request.headers.get('X-CSRF-Token', '')
        expected = session.get('integration_csrf', '')
        if not token or not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            abort(400, 'Invalid form token. Reload the page and retry.')

    def allowed(endpoint, method='GET'):
        return not enabled() or (bool(getattr(g, 'access_user', None)) and
            ROLES.index(g.access_user['role']) >= ROLES.index(required_role(endpoint, method)))

    @app.before_request
    def access_guard():
        g.access_user = None
        if not enabled():
            if (request.endpoint or '').startswith('access_') and request.endpoint != 'access_settings':
                abort(403, 'Provision a local administrator on the server first.')
            return
        if request.endpoint == 'static' or request.endpoint is None:
            return
        if request.endpoint in PUBLIC:
            return
        identity = session.get('access_identity') or {}
        user = store.get(identity.get('id', ''))
        if (user and user['active'] and user['version'] == identity.get('version')
                and 0 <= time.time() - identity.get('at', 0) < 8 * 3600):
            g.access_user = {key: value for key, value in user.items() if key != 'password'}
        else:
            session.pop('access_identity', None)
            if request.path.startswith('/api/') or request.method not in ('GET', 'HEAD'):
                return jsonify(error='Authentication required'), 401
            return redirect(url_for('access_login', next=request.full_path))
        if not allowed(request.endpoint, request.method):
            abort(403, 'Your role does not permit this action.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            csrf()

    # Run before existing integration CSRF checks, so permission errors take precedence.
    app.before_request_funcs[None].insert(0, app.before_request_funcs[None].pop())

    @app.context_processor
    def access_context():
        return {'access_enabled': enabled(), 'access_user': getattr(g, 'access_user', None),
                'access_can': allowed, 'access_roles': ROLES}

    @app.after_request
    def access_response(response):
        if not enabled():
            return response
        response.headers['Cache-Control'] = 'no-store'
        if response.mimetype == 'text/html' and not response.direct_passthrough:
            # Server-rendered tokens also cover fetch(FormData) and newly replaced page fragments.
            session.setdefault('integration_csrf', secrets.token_urlsafe(32))
            token = session['integration_csrf']
            def form(match):
                opening, contents = match.group(1), match.group(2)
                if not re.search(r'\bmethod\s*=\s*[\"\']?post\b', opening, re.I):
                    return match.group(0)
                action = re.search(r'\baction\s*=\s*[\"\']([^\"\']*)', opening, re.I)
                path = urlsplit(action.group(1)).path if action else request.path
                try:
                    endpoint, _ = app.url_map.bind_to_environ(request.environ).match(path, method='POST')
                except Exception:
                    endpoint = None
                if getattr(g, 'access_user', None) and not allowed(endpoint, 'POST'):
                    return '<p class="field-help">Your role has read-only access to these controls.</p>'
                return opening + '<input type="hidden" name="integration_csrf" value="' + token + '">' + contents + '</form>'
            if request.endpoint != 'simulator_controller':
                response.set_data(re.sub(r'(<form\b[^>]*>)(.*?)</form>', form, response.get_data(as_text=True), flags=re.I | re.S))
        return response

    def sign_in(user):
        session.clear()
        session['access_identity'] = {'id': user['id'], 'version': user['version'], 'at': time.time()}
        session['integration_csrf'] = secrets.token_urlsafe(32)

    def safe_next(value):
        return value if value and value.startswith('/') and not value.startswith('//') and '\\' not in value else '/'

    @app.route('/login', methods=['GET', 'POST'], endpoint='access_login')
    def login():
        if request.method == 'POST':
            csrf()
            key = (request.remote_addr or '', request.form.get('username', '').strip().lower()[:80])
            now = time.monotonic()
            with attempts_lock:
                for old in [k for k, value in attempts.items() if now - value[0] > 900]:
                    attempts.pop(old, None)
                count = attempts.get(key, (now, 0))
                if count[1] >= 10:
                    abort(429, 'Too many login attempts. Retry in 15 minutes.')
                if len(attempts) > 10000:
                    abort(429, 'Login temporarily rate limited.')
                attempts[key] = (count[0], count[1] + 1)
            password = request.form.get('password', '')
            user = store.authenticate(request.form.get('username', '')[:80], password[:257])
            if user:
                with attempts_lock:
                    attempts.pop(key, None)
                sign_in(user)
                return redirect(safe_next(request.form.get('next')))
            flash('Invalid username or password.', 'error')
        return render_template('login.html', oidc=store.oidc().get('enabled'), next_url=safe_next(request.args.get('next')))

    def oidc_client(settings):
        # Lazy import keeps upgrades usable before the optional SSO dependencies are installed.
        from authlib.integrations.flask_client import OAuth
        oauth = OAuth(app)
        return oauth.register('authentik', client_id=settings['client_id'], client_secret=settings['client_secret'],
            server_metadata_url=settings['issuer'].rstrip('/') + '/.well-known/openid-configuration',
            client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256', 'timeout': 10})

    @app.get('/auth/oidc/login', endpoint='access_oidc_login')
    def oidc_login():
        settings = store.oidc()
        if not settings.get('enabled'):
            abort(404)
        try:
            client = oidc_client(settings)
            metadata = client.load_server_metadata()
            if metadata.get('issuer') != settings['issuer']:
                raise ValueError('Issuer mismatch')
            for field in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
                if urlsplit(metadata.get(field, '')).scheme != 'https':
                    raise ValueError('OIDC endpoints must use HTTPS')
            return client.authorize_redirect(settings['redirect_uri'], nonce=secrets.token_urlsafe(32))
        except Exception:
            app.logger.warning('OIDC authorization could not start')
            abort(503, 'Authentik login is unavailable. Use local recovery access or check the OIDC configuration.')

    @app.get('/auth/oidc/callback', endpoint='access_oidc_callback')
    def oidc_callback():
        settings = store.oidc()
        if not settings.get('enabled'):
            abort(404)
        try:
            token = oidc_client(settings).authorize_access_token()
            if not token.get('id_token') or not token.get('userinfo'):
                raise ValueError('Validated ID token required')
            user = store.oidc_user(dict(token['userinfo']), settings)
        except Exception:
            app.logger.warning('OIDC callback validation or role assignment failed')
            abort(403, 'Authentik sign-in failed or no NetEm role is assigned.')
        sign_in(user)
        return redirect('/')

    @app.post('/logout', endpoint='access_logout')
    def logout():
        session.clear()
        return redirect(url_for('access_login'))

    @app.get('/settings/users', endpoint='access_settings')
    def settings_page():
        settings = dict(store.oidc())
        settings.pop('client_secret', None)
        return render_template('users.html', page='settings', users=store.users() if enabled() else [],
                               oidc=settings, group_mapping=json.dumps(settings.get('groups', {}), indent=2))

    @app.post('/settings/users/create', endpoint='access_create')
    def create_user():
        try:
            store.create(request.form.get('username', ''), request.form.get('password', ''), request.form.get('role'))
            flash('User created.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('access_settings'))

    @app.post('/settings/users/update', endpoint='access_update')
    def update_user():
        try:
            store.update(request.form.get('user_id'), request.form.get('role'),
                         request.form.get('active') == '1', request.form.get('password', ''))
            flash('User updated. Existing logins for this account have been revoked.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('access_settings'))

    @app.post('/settings/oidc', endpoint='access_oidc_settings')
    def oidc_settings():
        try:
            settings = {'enabled': request.form.get('enabled') == '1',
                'issuer': request.form.get('issuer', '').strip(),
                'redirect_uri': request.form.get('redirect_uri', '').strip(),
                'client_id': request.form.get('client_id', '').strip(),
                'client_secret': request.form.get('client_secret') or store.oidc().get('client_secret', ''),
                'groups': json.loads(request.form.get('groups', '{}'))}
            store.save_oidc(settings)
            flash('OIDC configuration saved.', 'success')
        except (ValueError, TypeError) as exc:
            flash(str(exc), 'error')
        return redirect(url_for('access_settings'))
    return store


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['create-admin', 'reset-password'])
    parser.add_argument('username')
    parser.add_argument('--database', default=str(Path(__file__).parent / 'runtime' / 'access.sqlite3'))
    args = parser.parse_args()
    password = getpass.getpass('New password (12+ characters): ')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords do not match.')
    access = AccessStore(args.database)
    try:
        if args.command == 'create-admin':
            access.create(args.username, password, 'admin')
        else:
            user = next((u for u in access.users() if not u['issuer'] and u['username'] == args.username.lower()), None)
            if not user:
                raise ValueError('Local user not found.')
            access.update(user['id'], user['role'], True, password)
    except ValueError as exc:
        parser.error(str(exc))
    print('Local account ready. Authentication is enabled; restart NetEm if necessary.')
