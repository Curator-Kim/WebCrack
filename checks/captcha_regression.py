"""验证码识别（ddddocr 可选依赖）回归：离线样例 + 临时回环 HTTP 站点。"""
import base64
import contextlib
import csv
import io
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import requests

import captcha_solver
import webcrack
from conf.config import captchaConfig, crackConfig, generatorConfig, parserConfig
from crack.crack_task import CaptchaSolveError, CrackTask
from http_requests import TaskStopped
from parse.parser import ParseIssue, Parser

CAPTCHA_CODE = 'AB12'
# 1x1 透明 PNG，仅用于让 requests 拿到非空图片字节。
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==')

CAPTCHA_FORM = ('<form method="post" action="/session">'
                '<input name="username"><input type="password" name="password">'
                '<label>验证码</label><input name="captcha">'
                '<img id="captchaImg" src="/captcha.png"></form>')


def response(body, status=200, content_type='text/html'):
    res = requests.Response()
    res.status_code = status
    res._content = body.encode()
    res.encoding = 'utf-8'
    res.url = 'https://example.test/login'
    res.headers['Content-Type'] = content_type
    return res


def image_response(payload=PNG):
    res = requests.Response()
    res.status_code = 200
    res._content = payload
    res.encoding = 'utf-8'
    res.url = 'https://example.test/captcha.png'
    res.headers['Content-Type'] = 'image/png'
    return res


class FakeSession:
    def __init__(self, posts=None, images=None):
        self.posts = []
        self.gets = []
        self._responses = list(posts or [])
        self._images = list(images or [])

    def get(self, url, **kwargs):
        self.gets.append(url)
        payload = self._images.pop(0) if self._images else PNG
        return image_response(payload)

    def post(self, url, **kwargs):
        payload = kwargs.get('data') if 'data' in kwargs else kwargs.get('json')
        self.posts.append(deepcopy(payload))
        return self._responses.pop(0)


def captcha_parser(content=CAPTCHA_FORM):
    parser = Parser('https://example.test/login')
    parser.response_url = parser.url
    parser.resp_content = content
    parser.form_parser()
    parser.check_login_page()
    return parser


class SolverModuleTests(unittest.TestCase):
    def test_data_uri_decoding(self):
        payload = base64.b64encode(b'fixture-bytes').decode()
        uri = 'data:image/png;base64,' + payload
        self.assertEqual(captcha_solver.decode_data_uri(uri), b'fixture-bytes')
        self.assertIsNone(captcha_solver.decode_data_uri('https://example.test/captcha.png'))
        self.assertIsNone(captcha_solver.decode_data_uri('data:image/png,notbase64'))
        self.assertIsNone(captcha_solver.decode_data_uri(None))

    def test_recognize_rejects_empty_input(self):
        self.assertIsNone(captcha_solver.recognize(b''))
        self.assertIsNone(captcha_solver.recognize(None))


class ParserCaptchaTests(unittest.TestCase):
    def test_no_captcha_keyword_leaves_state_unset(self):
        html = '<form><input name="username"><input type="password" name="password"></form>'
        parser = captcha_parser(html)
        self.assertFalse(parser.captcha_parser())
        self.assertFalse(parser.captcha_required)

    def test_detects_field_and_resolves_image_url(self):
        with patch.object(captcha_solver, 'available', return_value=True):
            parser = captcha_parser()
            self.assertTrue(parser.captcha_parser())
        self.assertTrue(parser.captcha_required)
        self.assertEqual(parser.captcha_field, 'captcha')
        self.assertEqual(parser.captcha_image_url, 'https://example.test/captcha.png')

    def test_weak_keyword_used_only_without_strong_match(self):
        html = ('<form><input name="username"><input type="password" name="password">'
                '验证码<input name="checkCode"><img src="/captcha.png"></form>')
        with patch.object(captcha_solver, 'available', return_value=True):
            parser = captcha_parser(html)
            self.assertTrue(parser.captcha_parser())
        self.assertEqual(parser.captcha_field, 'checkCode')

    def test_missing_engine_raises_required(self):
        with patch.object(captcha_solver, 'available', return_value=False):
            parser = captcha_parser()
            with self.assertRaises(ParseIssue) as raised:
                parser.captcha_parser()
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')
        self.assertFalse(parser.captcha_required)

    def test_disabled_by_config_raises_required(self):
        with patch.object(captcha_solver, 'available', return_value=True), \
                patch.dict(captchaConfig, {'enable': False}):
            parser = captcha_parser()
            with self.assertRaises(ParseIssue) as raised:
                parser.captcha_parser()
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')

    def test_missing_image_raises_required(self):
        html = ('<form><input name="username"><input type="password" name="password">'
                '验证码<input name="captcha"></form>')
        with patch.object(captcha_solver, 'available', return_value=True):
            parser = captcha_parser(html)
            with self.assertRaises(ParseIssue) as raised:
                parser.captcha_parser()
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')

    def test_missing_field_raises_required(self):
        html = ('<form><input name="username"><input type="password" name="password">'
                '验证码<img id="captchaImg" src="/captcha.png"></form>')
        with patch.object(captcha_solver, 'available', return_value=True):
            parser = captcha_parser(html)
            with self.assertRaises(ParseIssue) as raised:
                parser.captcha_parser()
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')

    def test_soft_mode_ignores_unlocatable_keyword(self):
        html = ('<form><input name="username"><input type="password" name="password">'
                '验证码</form>')
        parser = captcha_parser(html)
        self.assertFalse(parser.captcha_parser(soft=True))
        self.assertFalse(parser.captcha_required)

    def test_soft_mode_still_requires_engine_when_solvable(self):
        with patch.object(captcha_solver, 'available', return_value=False):
            parser = captcha_parser()
            with self.assertRaises(ParseIssue) as raised:
                parser.captcha_parser(soft=True)
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')

    def test_script_text_does_not_trigger_detection(self):
        html = ('<form><input name="username"><input type="password" name="password">'
                '<script>var message="captcha";</script></form>')
        parser = captcha_parser(html)
        self.assertFalse(parser.captcha_parser())

    def test_refresh_redetects_captcha(self):
        with patch.object(captcha_solver, 'available', return_value=True):
            template = captcha_parser()
            template.captcha_parser()
            template.param_parser()
            template.post_path_parser()
            template.diagnostic_code = 'FORM_FOUND'

            def get_page(parser):
                parser.response_url = parser.url
                parser.resp_content = CAPTCHA_FORM

            refreshed = Parser(template.url)
            with patch.object(Parser, 'get_resp_content', get_page):
                self.assertTrue(refreshed.refresh_from(template))
        self.assertTrue(refreshed.captcha_required)
        self.assertEqual(refreshed.captcha_field, 'captcha')
        self.assertEqual(refreshed.diagnostic_code, 'PLAN_REFRESHED')

    def test_profile_captcha_requires_engine(self):
        profile = {'page_url': 'https://example.test/login', 'endpoint': '/session',
                   'username_field': 'username', 'password_field': 'password',
                   'success_fields': {'authenticated': True},
                   'captcha': {'field': 'captcha', 'image_url': '/captcha.png'}}
        parser = Parser('https://example.test/login')
        with patch.dict(parserConfig, {'site_profiles': [profile]}), \
                patch.object(captcha_solver, 'available', return_value=True):
            self.assertTrue(parser.apply_site_profile())
            self.assertTrue(parser.captcha_required)
            self.assertEqual(parser.captcha_field, 'captcha')

    def test_profile_captcha_without_engine_raises(self):
        profile = {'page_url': 'https://example.test/login', 'endpoint': '/session',
                   'username_field': 'username', 'password_field': 'password',
                   'success_fields': {}, 'captcha': {'field': 'captcha', 'image_url': '/captcha.png'}}
        parser = Parser('https://example.test/login')
        with patch.dict(parserConfig, {'site_profiles': [profile]}), \
                patch.object(captcha_solver, 'available', return_value=False):
            with self.assertRaises(ParseIssue) as raised:
                parser.apply_site_profile()
        self.assertEqual(raised.exception.code, 'CAPTCHA_REQUIRED')


class CrackRequestCaptchaTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(crackConfig, {'delay': 0, 'concurrency': 1})
        self.settings.start()

    def tearDown(self):
        self.settings.stop()

    def task(self, **overrides):
        parser = SimpleNamespace(
            data={'username': '', 'password': '', 'captcha': '0000'},
            username_keyword='username', password_keyword='password',
            post_path='https://example.test/session', request_format='form',
            request_headers={}, cms={},
            captcha_required=True, captcha_field='captcha',
            captcha_image_url='https://example.test/captcha.png')
        for key, value in overrides.items():
            setattr(parser, key, value)
        task = CrackTask()
        task.url = 'https://example.test/login'
        task.parser = parser
        return task

    def test_recognized_text_injected_into_form_data(self):
        conn = FakeSession(posts=[response('密码错误')])
        with patch.object(captcha_solver, 'recognize', return_value='A7B9'):
            self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual(len(conn.posts), 1)
        self.assertEqual(conn.posts[0]['captcha'], 'A7B9')
        self.assertEqual(conn.posts[0]['username'], 'admin')
        self.assertEqual(conn.gets, ['https://example.test/captcha.png'])

    def test_recognized_text_injected_into_json_data(self):
        conn = FakeSession(posts=[response('{"authenticated":false}', content_type='application/json')])
        with patch.object(captcha_solver, 'recognize', return_value='Q1W2'):
            self.task(request_format='json').crack_request(conn, 'admin', 'secret')
        self.assertEqual(conn.posts[0]['captcha'], 'Q1W2')

    def test_retries_with_new_captcha_after_rejection(self):
        conn = FakeSession(posts=[response('验证码错误'), response('密码错误')])
        with patch.object(captcha_solver, 'recognize', side_effect=['AAAA', 'BBBB']):
            self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual([post['captcha'] for post in conn.posts], ['AAAA', 'BBBB'])
        self.assertEqual(len(conn.gets), 2)

    def test_rejection_is_not_retried_when_not_captcha_site(self):
        conn = FakeSession(posts=[response('验证码错误')])
        with patch.object(captcha_solver, 'recognize') as recognize:
            self.task(captcha_required=False).crack_request(conn, 'admin', 'secret')
            recognize.assert_not_called()
        self.assertEqual(len(conn.posts), 1)

    def test_solve_failure_raises_after_retries(self):
        conn = FakeSession(posts=[response('ok')])
        with patch.dict(captchaConfig, {'solve_retries': 3}), \
                patch.object(captcha_solver, 'recognize', return_value=None):
            with self.assertRaises(CaptchaSolveError):
                self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual(len(conn.gets), 3)
        self.assertEqual(conn.posts, [])

    def test_inline_data_uri_image_is_not_requested(self):
        payload = base64.b64encode(PNG).decode()
        conn = FakeSession(posts=[response('密码错误')])
        task = self.task(captcha_image_url='data:image/png;base64,' + payload)
        with patch.object(captcha_solver, 'recognize', return_value='ZZ99'):
            task.crack_request(conn, 'admin', 'secret')
        self.assertEqual(conn.gets, [])
        self.assertEqual(conn.posts[0]['captcha'], 'ZZ99')

    def test_invalid_length_rejected(self):
        conn = FakeSession(posts=[response('ok')])
        with patch.dict(captchaConfig, {'solve_retries': 1, 'max_length': 3}), \
                patch.object(captcha_solver, 'recognize', return_value='TOOLONG'):
            with self.assertRaises(CaptchaSolveError):
                self.task().crack_request(conn, 'admin', 'secret')

    def test_captcha_cancellation_propagates(self):
        class StoppingSession(FakeSession):
            def get(self, url, **kwargs):
                raise TaskStopped('任务已停止，取消请求及重试')

        task = self.task()
        task._stop_event = threading.Event()
        with patch.object(captcha_solver, 'recognize') as recognize, \
                self.assertRaises(TaskStopped):
            task.fetch_captcha_image(StoppingSession())
        recognize.assert_not_called()

    def test_transient_5xx_is_retried(self):
        conn = FakeSession(posts=[response('oops', 500), response('oops', 503), response('密码错误')])
        with patch.object(captcha_solver, 'recognize', return_value='A1B2'):
            res = self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(conn.posts), 3)
        self.assertEqual(len(conn.gets), 3)

    def test_persistent_5xx_returns_last_response(self):
        conn = FakeSession(posts=[response('oops', 500), response('oops', 500), response('oops', 500)])
        with patch.dict(crackConfig, {'server_error_retries': 2}), \
                patch.object(captcha_solver, 'recognize', return_value='A1B2'):
            res = self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual(res.status_code, 500)
        self.assertEqual(len(conn.posts), 3)

    def test_server_retry_disabled(self):
        conn = FakeSession(posts=[response('oops', 500)])
        with patch.dict(crackConfig, {'server_error_retries': 0}), \
                patch.object(captcha_solver, 'recognize', return_value='A1B2'):
            res = self.task().crack_request(conn, 'admin', 'secret')
        self.assertEqual(res.status_code, 500)
        self.assertEqual(len(conn.posts), 1)

    def test_run_abandons_site_on_solve_failure(self):
        with patch('crack.crack_task.Parser') as parser, \
                patch.object(CrackTask, 'get_error_length',
                             side_effect=CaptchaSolveError('验证码识别失败')), \
                contextlib.redirect_stdout(io.StringIO()):
            parser.return_value.run.return_value = True
            self.assertIsNone(self.task().run(1, 'https://example.test/login'))


@contextmanager
def running_captcha_site(events):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, kind='text/html; charset=utf-8', extra=None):
            raw = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            events.append(('GET', self.path))
            if self.path in ('/login', '/'):
                self.send(200, CAPTCHA_FORM)
            elif self.path.startswith('/captcha.png'):
                self.send(200, PNG, 'image/png', {'Set-Cookie': 'fixture_captcha=%s; Path=/' % CAPTCHA_CODE})
            else:
                self.send(404, 'Not found')

        def do_POST(self):
            events.append(('POST', self.path))
            if self.path != '/session':
                self.send(404, 'Not found')
                return
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()
            data = {key: value[0] for key, value in parse_qs(raw).items()}
            if data.get('captcha') != CAPTCHA_CODE:
                self.send(200, CAPTCHA_FORM + '<p>验证码错误</p>')
            elif (data.get('username'), data.get('password')) == ('admin', '123456'):
                self.send(200, '<h1>FIXTURE_AUTHENTICATED</h1>')
            else:
                self.send(200, CAPTCHA_FORM + '<p>密码错误</p>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                     'no_proxy': '127.0.0.1,localhost'}):
            yield 'http://127.0.0.1:%d' % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CaptchaIntegrationTests(unittest.TestCase):
    def run_main(self, base, recognize, output):
        events = []
        base_dict = generatorConfig['dict_config']['base_dict']
        with running_captcha_site(events) as site:
            url = site + '/login'
            with patch.object(captcha_solver, 'available', return_value=True), \
                    patch.object(captcha_solver, 'recognize', side_effect=recognize), \
                    patch.dict(crackConfig, {'success_words': ['FIXTURE_AUTHENTICATED'],
                                            'json_success_fields': {}, 'delay': 0, 'timeout': 3,
                                            'requests_proxies': {}, 'concurrency': 1}), \
                    patch.dict(base_dict, {'username_list': ['admin'],
                                           'password_list': ['nope', '123456']}), \
                    patch.dict(generatorConfig['dict_config']['domain_dict'], {'enable': False}), \
                    patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
                    patch.dict(generatorConfig['headers_config'], {'enable': False}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = webcrack.main(['-u', url, '-o', str(output), '-t', '1', '-c', '1'])
            report = {'url': url, 'exit': code, 'events': list(events)}
        return report

    def test_end_to_end_login_through_captcha(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'results.txt'
            report = self.run_main('', lambda image: CAPTCHA_CODE, output)
            with output.open(newline='') as handle:
                rows = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(report['exit'], 0)
        self.assertEqual(rows, [{'url': report['url'], 'username': 'admin', 'password': '123456'}])
        self.assertGreaterEqual(sum(1 for method, path in report['events']
                                    if path.startswith('/captcha.png')), 2)

    def test_wrong_recognition_yields_no_result(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'results.txt'
            self.run_main('', lambda image: 'WRONG', output)
            with output.open(newline='') as handle:
                rows = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(rows, [])

    def test_missing_engine_skips_captcha_site(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'results.txt'
            events = []
            with running_captcha_site(events) as site:
                base_dict = generatorConfig['dict_config']['base_dict']
                with patch.object(captcha_solver, 'available', return_value=False), \
                        patch.dict(crackConfig, {'success_words': ['FIXTURE_AUTHENTICATED'],
                                                'delay': 0, 'timeout': 3, 'requests_proxies': {},
                                                'concurrency': 1}), \
                        patch.dict(base_dict, {'username_list': ['admin'], 'password_list': ['123456']}), \
                        patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
                        patch.dict(generatorConfig['headers_config'], {'enable': False}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = webcrack.main(['-u', site + '/login', '-o', str(output), '-c', '1'])
                with output.open(newline='') as handle:
                    rows = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(code, 0)
        self.assertEqual(rows, [])
        self.assertFalse([path for method, path in events if path.startswith('/captcha.png')])

    def test_cli_no_captcha_disables_recognition(self):
        self.addCleanup(captchaConfig.__setitem__, 'enable', True)
        with patch.object(webcrack, 'CrackTask') as task, \
                contextlib.redirect_stdout(io.StringIO()), \
                patch.object(webcrack, 'write_results'), \
                patch.dict(crackConfig, {'requests_proxies': {}}):
            self.assertEqual(webcrack.main(['-u', 'https://example.test/', '--no-captcha']), 0)
            self.assertFalse(captchaConfig['enable'])


if __name__ == '__main__':
    unittest.main()
