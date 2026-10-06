"""Regression tests for login invalidation and expired-form recovery."""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from flask import Flask
from werkzeug.security import generate_password_hash

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('login_auth_under_test', BASE / 'auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)
HASH = generate_password_hash('test-password-123')

class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__, template_folder=str(BASE/'templates'))
        self.access = auth.AccessControl(self.app)
        self.access.configure(Path(self.tmp.name)/'auth.json', secure=True)
        self.access.path.write_text(json.dumps({'users':{'admin':{'role':'admin','password_hash':HASH}}}))
        self.app.add_url_rule('/', 'home', lambda:'Authenticated')
        self.app.add_url_rule('/api/status', 'status', lambda:{'ok':True})
        self.client = self.app.test_client()
    def tearDown(self):
        self.tmp.cleanup()
    def token(self, response):
        return re.search(r'name="csrf" value="([^"]*)"',response.text)[1]
    def submit(self, token):
        return self.client.post('/login',data={'username':'admin','password':'test-password-123','csrf':token})
    def test_favicon_does_not_break_login(self):
        token=self.token(self.client.get('/login'))
        response=self.client.get('/favicon.ico',follow_redirects=True)
        self.assertEqual(response.status_code,204)
        self.assertNotIn('Set-Cookie',response.headers)
        self.assertEqual(self.submit(token).status_code,302)
    def test_missing_asset_does_not_break_login(self):
        token=self.token(self.client.get('/login'))
        self.assertEqual(self.client.get('/missing-icon.png',follow_redirects=True).status_code,404)
        self.assertEqual(self.submit(token).status_code,302)
    def test_two_login_tabs_share_valid_form(self):
        token=self.token(self.client.get('/login'))
        self.assertEqual(self.token(self.client.get('/login')),token)
        self.assertEqual(self.submit(token).status_code,302)
    def test_expired_form_returns_working_token(self):
        token=self.token(self.client.get('/login'))
        self.access.sessions.clear()  # Same effect as restarting the server.
        failed=self.submit(token)
        self.assertEqual(failed.status_code,403)
        self.assertEqual(self.client.get('/api/status').status_code,401)
        new_token=self.token(failed)
        self.assertTrue(new_token)
        self.assertNotEqual(new_token,token)
        self.assertEqual(self.submit(new_token).status_code,302)
    def test_wrong_csrf_rejected_without_breaking_other_tab(self):
        token=self.token(self.client.get('/login'))
        failed=self.submit('invalid')
        self.assertEqual(failed.status_code,403)
        self.assertEqual(self.token(failed),token)
        self.assertEqual(self.submit(token).status_code,302)
    def test_login_get_keeps_authenticated_session(self):
        self.submit(self.token(self.client.get('/login')))
        self.assertEqual(self.client.get('/login').status_code,302)
        self.assertEqual(self.client.get('/api/status').status_code,200)

if __name__=='__main__':unittest.main()
