"""Local accounts, opaque server-side sessions and CSRF protection."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from flask import g, jsonify, redirect, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

AUTH_PATH = Path(__file__).resolve().parent / 'auth.json'
COOKIE = 'webchat_session'
IDLE_SECONDS = 30 * 60
MAX_SECONDS = 8 * 60 * 60


class AccessControl:
    def __init__(self, app):
        self.app = app
        self.path = AUTH_PATH
        self.secure = True
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = {}
        app.before_request(self.guard)
        app.after_request(self.headers)
        app.add_url_rule('/login', 'login', self.login, methods=['GET', 'POST'])
        app.add_url_rule('/logout', 'logout', self.logout, methods=['POST'])

    def configure(self, path=AUTH_PATH, secure=True):
        with self.lock:
            self.path = Path(path)
            self.secure = bool(secure)
            self.sessions.clear()
            self.failures.clear()

    def accounts(self):
        try:
            data = json.loads(self.path.read_text())
            return data.get('users', {})
        except (OSError, ValueError, AttributeError):
            return {}

    def session(self):
        now = time.monotonic()
        sid = request.cookies.get(COOKIE, '')
        with self.lock:
            for key, entry in list(self.sessions.items()):
                if now-entry['last'] > IDLE_SECONDS or now-entry['created'] > MAX_SECONDS:
                    self.sessions.pop(key, None)
            entry = self.sessions.get(sid)
            if entry and entry.get('user'):
                current = self.accounts().get(entry['user'], {})
                version = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
                if version != entry['account_version']:
                    self.sessions.pop(sid, None)
                    return None
            if entry:
                entry['last'] = now
            return entry

    def new_session(self, user=None, account=None):
        sid = secrets.token_urlsafe(32)
        now = time.monotonic()
        entry = dict(user=user, role=(account or {}).get('role'), csrf=secrets.token_urlsafe(32),
                     created=now, last=now,
                     account_version=hashlib.sha256(json.dumps(account or {}, sort_keys=True).encode()).hexdigest())
        with self.lock:
            self.sessions.pop(request.cookies.get(COOKIE, ''), None)
            if len(self.sessions) >= 2048:
                self.sessions.pop(min(self.sessions, key=lambda k:self.sessions[k]['last']))
            self.sessions[sid] = entry
        return sid, entry

    def set_cookie(self, response, sid):
        response.set_cookie(COOKIE, sid, secure=self.secure, httponly=True,
                            samesite='Strict', max_age=MAX_SECONDS, path='/')
        return response

    def guard(self):
        if request.path == '/login':
            return None
        entry = self.session()
        if not entry or not entry.get('user'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Authentication required'), 401
            return redirect('/login')
        g.auth = entry
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token, entry['csrf']):
                return jsonify(ok=False, error='Invalid CSRF token'), 403
            if request.path != '/logout' and entry['role'] != 'admin':
                return jsonify(ok=False, error='Read-only account'), 403
        if entry['role'] != 'admin' and request.path in {'/api/config/export','/api/debug'}:
            return jsonify(ok=False, error='Administrator access required'), 403
        if entry['role'] != 'admin' and request.path == '/api/rooms/active' and request.args.get('refresh','').lower() in {'1','true','yes'}:
            return jsonify(ok=False, error='Read-only account'), 403

    def headers(self, response):
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def login(self):
        users = self.accounts()
        if not users:
            return render_template('login.html', setup_required=True), 503
        if request.method == 'GET':
            sid, entry = self.new_session()
            return self.set_cookie(self.app.make_response(render_template('login.html', csrf=entry['csrf'])), sid)
        entry = self.session()
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return jsonify(ok=False, error='Cross-origin request rejected'), 403
        if not entry or not secrets.compare_digest(request.form.get('csrf',''), entry['csrf']):
            return render_template('login.html', error='Login expired. Reload this page.', csrf=''), 403
        now = time.monotonic()
        ip = request.remote_addr or 'unknown'
        with self.lock:
            self.failures = {k:v for k,v in self.failures.items() if now-v['start'] < 300}
            attempt = self.failures.setdefault(ip, {'start':now, 'count':0})
            if attempt['count'] >= 5:
                return render_template('login.html', error='Too many attempts. Try again in five minutes.', csrf=entry['csrf']), 429
            # Count before checking the hash, including concurrent requests.
            attempt['count'] += 1
            if len(self.failures) > 4096:
                self.failures.pop(next(iter(self.failures)))
        username = request.form.get('username','')[:80]
        password = request.form.get('password','')
        account = users.get(username, {})
        valid = False
        if len(password) <= 1024 and account.get('role') in {'admin','viewer'}:
            try:
                valid = check_password_hash(account.get('password_hash',''), password)
            except (ValueError, TypeError):
                pass
        if not valid:
            return render_template('login.html', error='Invalid username or password.', csrf=entry['csrf']), 401
        with self.lock:
            self.failures.pop(ip, None)
        sid, _entry = self.new_session(username, account)
        return self.set_cookie(redirect('/'), sid)

    def logout(self):
        with self.lock:
            self.sessions.pop(request.cookies.get(COOKIE,''), None)
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, path='/', secure=self.secure, httponly=True, samesite='Strict')
        return response


def main():
    parser = argparse.ArgumentParser(description='Create or update a local Web Chat account')
    parser.add_argument('--user', required=True)
    parser.add_argument('--role', choices=['admin','viewer'], default='admin')
    args = parser.parse_args()
    if not args.user or len(args.user) > 80:
        parser.error('Username must contain 1 to 80 characters')
    password = getpass.getpass('Password (at least 12 characters): ')
    if not 12 <= len(password) <= 1024:
        parser.error('Password must contain 12 to 1024 characters')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords do not match')
    data = json.loads(AUTH_PATH.read_text()) if AUTH_PATH.exists() else {'users': {}}
    data['users'][args.user] = {'role':args.role, 'password_hash':generate_password_hash(password, method='scrypt')}
    # Exclusive temp creation prevents following an existing symlink.
    tmp = AUTH_PATH.with_name('.auth.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, AUTH_PATH)
    print(f'Account {args.user} saved with role {args.role}.')

if __name__ == '__main__':
    main()
