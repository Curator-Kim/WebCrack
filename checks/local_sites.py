"""Two disposable loopback-only login sites, implemented with Python stdlib."""
import json
import secrets
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ACCOUNTS = {'html': ('admin', '123456'), 'ajax': ('admin', 'admin123')}
HTML = '''<!doctype html><html><body><form method="post" action="/session">
<input name="username"><input type="password" name="password"><button>Login</button>
</form></body></html>'''
AJAX = '''<!doctype html><html><body><div id="login">
<input id="username"><input id="password" type="password"><button onclick="login()">Login</button>
</div><script>
async function login() {
 const res = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
 body:JSON.stringify({username:document.getElementById('username').value,
 password:document.getElementById('password').value})});
 const result = await res.json(); if(result.authenticated) location.href='/dashboard';
}
</script></body></html>'''


def handler_for(kind, events):
    sessions = set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='text/html; charset=utf-8', extra=None):
            body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            return 'fixture_session' in cookie and cookie['fixture_session'].value in sessions

        def do_GET(self):
            events.append({'site': kind, 'method': 'GET', 'path': self.path})
            if self.path == '/login':
                self.send(200, HTML if kind == 'html' else AJAX)
            elif self.path == '/dashboard':
                if self.authenticated():
                    self.send(200, '<h1>FIXTURE_AUTHENTICATED</h1><a href="/logout">Logout</a>')
                else:
                    self.send(302, '', extra={'Location': '/login'})
            else:
                self.send(404, 'Not found')

        def do_POST(self):
            events.append({'site': kind, 'method': 'POST', 'path': self.path})
            expected_path = '/session' if kind == 'html' else '/api/login'
            if self.path != expected_path:
                self.send(404, 'Not found')
                return
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()
            if kind == 'ajax':
                if 'application/json' not in self.headers.get('Content-Type', ''):
                    self.send(415, '{"error":"JSON required"}', 'application/json')
                    return
                try:
                    data = json.loads(raw)
                except ValueError:
                    self.send(400, '{"error":"Invalid JSON"}', 'application/json')
                    return
            else:
                data = {k: v[0] for k, v in parse_qs(raw).items()}
            valid = (data.get('username'), data.get('password')) == ACCOUNTS[kind]
            if valid:
                token = secrets.token_hex(16)
                sessions.add(token)
                headers = {'Set-Cookie': f'fixture_session={token}; HttpOnly; SameSite=Lax; Path=/'}
                if kind == 'html':
                    headers['Location'] = '/dashboard'
                    self.send(302, '', extra=headers)
                else:
                    self.send(200, '{"authenticated":true}', 'application/json', headers)
            elif kind == 'html':
                self.send(200, HTML + '<p>密码错误</p>' + '.' * secrets.randbelow(8))
            else:
                self.send(200, '{"authenticated":false,"error":"密码错误"}', 'application/json')
    return Handler


@contextmanager
def running_sites():
    events = []
    servers = {kind: ThreadingHTTPServer(('127.0.0.1', 0), handler_for(kind, events))
               for kind in ACCOUNTS}
    threads = []
    try:
        for server in servers.values():
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        yield {kind: f'http://127.0.0.1:{server.server_port}' for kind, server in servers.items()}, events
    finally:
        for server in servers.values():
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
