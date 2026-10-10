import contextlib
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import json
import requests
from conf.config import crackConfig, generatorConfig, parserConfig
from parse.parser import Parser
from crack.crack_task import CrackTask, LoginState
from checks.login_regression import response

SHELL = '<div id="app"></div><script type="module" src="/assets/js/index-hash.js"></script>'
BUNDLE = '''const api=axios.create({baseURL:"/api",timeout:1e4});
api.interceptors.response.use(e=>{const{code:t,message:n,data:a}=e.data;return t===200?a:Promise.reject(n)});
const auth={login:async e=>api.post("/auth/login",e)};
const model=reactive({username:"",password:""});
async function submit(){const token=await auth.login(model);localStorage.setItem("token",token);}
'''


class AxiosTests(unittest.TestCase):
    def parser(self):
        p = Parser('http://example.test/procurement/login')
        p.response_url = p.url
        return p

    def test_success_condition_inferred_without_wrapper(self):
        script = ("axios.post('/auth/login',{username:u.value,password:p.value})"
                  ".then(res=>{ if(res.data.success){location.href='/home';}else{show(res.data.message);} });")
        parser = Parser('http://example.test/login')
        parser.response_url = parser.url
        parser.resp_content = ('<form><input name="username"><input type="password" name="password"></form>'
                               '<script>' + script + '</script>')
        self.assertTrue(parser.json_login_parser())
        self.assertEqual(parser.post_path, 'http://example.test/auth/login')
        self.assertEqual(parser.json_response_success, {'data.success': [True]})

    def test_endpoint_and_wrapped_success(self):
        p = self.parser()
        self.assertTrue(p.axios_login_parser([BUNDLE]))
        self.assertEqual(p.post_path, 'http://example.test/api/auth/login')
        self.assertEqual(p.request_format, 'json')
        self.assertEqual(p.json_required_nonempty_fields, ['data'])
        task = CrackTask()
        task.parser = p
        for body, expected in (({'code': 200, 'data': 'fixture-token'}, LoginState.SUCCESS),
                               ({'code': 200, 'data': None}, LoginState.UNKNOWN),
                               ({'code': 200, 'data': ''}, LoginState.UNKNOWN),
                               ({'code': 200, 'data': True}, LoginState.UNKNOWN),
                               ({'code': '200', 'data': 'fixture-token'}, LoginState.UNKNOWN),
                               ({'code': 500, 'data': None, 'message': '密码错误'}, LoginState.FAILURE)):
            with self.subTest(body=body):
                self.assertEqual(task.classify_response(response(json.dumps(body, ensure_ascii=False), content_type='application/json')), expected)

    def test_no_guessing_missing_call_chain(self):
        for script in (BUNDLE.replace('auth.login(model)', 'unknown.login(model)'),
                       BUNDLE.replace('username:""', 'email:""'),
                       BUNDLE.replace('baseURL:"/api"', 'baseURL:"http://other.test/api"')):
            self.assertFalse(self.parser().axios_login_parser([script]))

    def test_module_large_bundle_real_http(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, body, content_type):
                raw = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                if self.path.endswith('.js'):
                    self.send('/*' + 'x' * 300000 + '*/' + BUNDLE, 'application/javascript')
                else:
                    self.send(SHELL, 'text/html')
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, self.headers.get('Content-Type')))
                success = data == {'username': 'fixture', 'password': 'fixturepass'}
                self.send(json.dumps({'code': 200 if success else 500, 'data': 'fixture-token' if success else None,
                                      'message': 'ok' if success else '密码错误'}, ensure_ascii=False), 'application/json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_session = requests.Session
        def session():
            s = real_session(); s.trust_env = False; return s
        try:
            url = f'http://127.0.0.1:{server.server_port}/procurement/login'
            with patch('requests.session', side_effect=session), \
                 patch.dict(crackConfig, {'delay': 0, 'requests_proxies': {}}), \
                 patch('crack.crack_task.gen_dict', return_value=(['fixture'], ['incorrect', 'fixturepass'])), \
                 patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
                 patch('crack.crack_task.Log'), contextlib.redirect_stdout(io.StringIO()):
                result = CrackTask().run(1, url)
            self.assertEqual(result, {'url': url, 'username': 'fixture', 'password': 'fixturepass'})
            self.assertTrue(all(path == '/api/auth/login' and kind == 'application/json' for path, kind in seen))
            # The configured byte limit is enforced; truncation is never interpreted as a full script.
            with session() as client, patch.dict(parserConfig, {'json_script_max_bytes': 1024}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(Parser(url, session=client).run())
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
