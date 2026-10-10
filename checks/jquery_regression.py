import contextlib
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from unittest.mock import patch

import requests
from conf.config import crackConfig, generatorConfig
from parse.parser import Parser, ParseIssue
from crack.crack_task import CrackTask, LoginState
from checks.login_regression import response

HTML = '''<form id="loginForm" method="post"><input name="userName"><input name="password" type="password">
<input name="ifRemember" type="checkbox"></form><script>var base_url = '/xxl-job-admin';</script>
<script src="/xxl-job-admin/static/js/login.1.js"></script>'''
SCRIPT = '''$.post(base_url + "/login", $("#loginForm").serialize(), function(data, status) {
 if (data.code == "200") { window.location.href = base_url + "/"; } else { console.log(data.msg); }
});'''


class JQueryTests(unittest.TestCase):
    def parser(self, script=SCRIPT):
        parser = Parser('http://example.test/xxl-job-admin/toLogin')
        parser.response_url = parser.url
        parser.resp_content = HTML
        parser.login_scripts = ["var base_url = '/xxl-job-admin';", script]
        parser.form_parser()
        parser.post_path_parser()
        parser.param_parser()
        return parser

    def test_ajaxsubmit_success_rule(self):
        html = ('<form id="form1" action="/login/loginAjax" method="post">'
                '<input name="userName"><input type="password" name="userPass"></form>')
        script = ('$("#form1").ajaxSubmit(function (ret) {\n'
                  '    Toast(ret.info, 3000);\n'
                  '    if (ret.success) { setTimeout("btnOpen()", 500); }\n'
                  '    else { btnValCode(); }\n'
                  '});')
        parser = Parser('https://example.test/login')
        parser.response_url = parser.url
        parser.resp_content = html
        parser.login_scripts = [script]
        parser.form_parser()
        parser.post_path_parser()
        parser.param_parser()
        self.assertTrue(parser.jquery_login_parser())
        self.assertEqual(parser.post_path, 'https://example.test/login/loginAjax')
        self.assertEqual(parser.json_response_success, {'success': [True]})

    def test_ajaxsubmit_falsy_or_other_form_ignored(self):
        html = ('<form id="loginForm" action="/login" method="post">'
                '<input name="userName"><input type="password" name="userPass"></form>')
        for script in ('$("#other").ajaxSubmit(function (ret) { if (ret.success) {} });',
                       '$("#loginForm").ajaxSubmit(function (ret) { if (!ret.success) {} });',
                       '$("#loginForm").ajaxSubmit(function (ret) { if (ret.success === false) {} });'):
            with self.subTest(script=script):
                parser = Parser('https://example.test/login')
                parser.response_url = parser.url
                parser.resp_content = html
                parser.login_scripts = [script]
                parser.form_parser()
                parser.post_path_parser()
                parser.param_parser()
                self.assertFalse(parser.jquery_ajaxsubmit_parser())
                self.assertEqual(parser.json_response_success, {})

    def test_ajaxsubmit_code_rule(self):
        html = ('<form id="form1" action="/login" method="post">'
                '<input name="user"><input type="password" name="pass"></form>')
        script = '$("#form1").ajaxSubmit(function (res) { if (res.code === 200) { location.reload(); } });'
        parser = Parser('https://example.test/login')
        parser.response_url = parser.url
        parser.resp_content = html
        parser.login_scripts = [script]
        parser.form_parser()
        parser.post_path_parser()
        parser.param_parser()
        self.assertTrue(parser.jquery_ajaxsubmit_parser())
        self.assertEqual(parser.json_response_success, {'code': [200, '200']})

    def test_post_success_condition_beyond_code(self):
        script = ("$.post('/xxl-job-admin/login', $('#loginForm').serialize(), function(data) {"
                  " if (data.success) { location.href='/'; } else { show(data.msg); } });")
        parser = self.parser(script)
        self.assertTrue(parser.jquery_login_parser())
        self.assertEqual(parser.json_response_success, {'success': [True]})

    def test_ajax_success_condition_beyond_code(self):
        script = ("$.ajax({url:'/xxl-job-admin/login',type:'POST',data:$('#loginForm').serialize(),"
                  "success:function(ret){ if(ret.authenticated){ location.href='/'; } }});")
        parser = self.parser(script)
        self.assertTrue(parser.jquery_login_parser())
        self.assertEqual(parser.json_response_success, {'authenticated': [True]})

    def test_endpoint_rules_and_unchecked_checkbox(self):
        parser = self.parser()
        self.assertTrue(parser.jquery_login_parser())
        self.assertEqual(parser.post_path, 'http://example.test/xxl-job-admin/login')
        self.assertEqual(parser.request_format, 'form')
        self.assertEqual(parser.json_response_success, {'code': [200, '200']})
        self.assertNotIn('ifRemember', parser.data)

    def test_wrong_form_and_unknown_variable(self):
        for script in (SCRIPT.replace('#loginForm', '#otherForm'), SCRIPT.replace('base_url +', 'unknown +')):
            self.assertFalse(self.parser(script).jquery_login_parser())

    def test_cross_origin_and_ambiguous(self):
        parser = self.parser(SCRIPT.replace('base_url + "/login"', '"http://other.test/login"'))
        self.assertFalse(parser.jquery_login_parser())
        parser = self.parser(SCRIPT + SCRIPT.replace('"/login"', '"/login2"'))
        with self.assertRaises(ParseIssue) as raised:
            parser.jquery_login_parser()
        self.assertEqual(raised.exception.code, "AMBIGUOUS_INTERFACE")

    def test_success_scope_and_strict_types(self):
        task = CrackTask()
        task.parser = self.parser()
        task.parser.jquery_login_parser()
        for body, expected in (('{"code":200}', LoginState.SUCCESS), ('{"code":"200"}', LoginState.SUCCESS),
                               ('{"code":500}', LoginState.UNKNOWN), ('{"code":true}', LoginState.UNKNOWN)):
            self.assertEqual(task.classify_response(response(body, content_type='application/json')), expected)
        task.parser.json_response_success = {}
        self.assertEqual(task.classify_response(response('{"code":200}', content_type='application/json')), LoginState.UNKNOWN)

    def test_real_http_end_to_end(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send(self, body, content_type='text/html'):
                self.send_response(200)
                self.send_header('Content-Type', content_type + '; charset=utf-8')
                self.end_headers()
                self.wfile.write(body.encode())
            def do_GET(self):
                self.send(SCRIPT if self.path.endswith('login.1.js') else HTML,
                          'application/javascript' if self.path.endswith('.js') else 'text/html')
            def do_POST(self):
                data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                seen.append((self.path, data))
                correct = data.get('userName') == ['fixture'] and data.get('password') == ['fixturepass']
                self.send('{"code":200,"msg":"ok"}' if correct else '{"code":500,"msg":"密码错误"}', 'application/json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_session = requests.Session
        def session():
            client = real_session()
            client.trust_env = False
            return client
        try:
            url = f'http://127.0.0.1:{server.server_port}/xxl-job-admin/toLogin'
            with patch('requests.session', side_effect=session), \
                 patch.dict(crackConfig, {'delay': 0, 'requests_proxies': {}}), \
                 patch('crack.crack_task.gen_dict', return_value=(['fixture'], ['wrong-fixture', 'fixturepass'])), \
                 patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
                 patch('crack.crack_task.Log'), contextlib.redirect_stdout(io.StringIO()):
                result = CrackTask().run(1, url)
            self.assertEqual(result, {'url': url, 'username': 'fixture', 'password': 'fixturepass'})
            self.assertTrue(seen)
            self.assertTrue(all(path == '/xxl-job-admin/login' and 'ifRemember' not in data for path, data in seen))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
