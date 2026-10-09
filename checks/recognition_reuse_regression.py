"""同 URL 的识别复用、动态表单刷新与缓存失效回归。"""
import contextlib
import io
import json
import secrets
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

import webcrack
from conf.config import parserConfig
from crack.crack_task import CrackTask
from parse.parser import Parser
from checks.jquery_regression import HTML, SCRIPT
from checks.request_concurrency_regression import fixture_settings, prepared_task


@contextmanager
def jquery_fixture():
    state = {'home': 0, 'pages': 0, 'scripts': 0, 'posts': [], 'invalid_tokens': 0}
    mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, kind='text/html', cookie=None):
            raw = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            if cookie:
                self.send_header('Set-Cookie', 'fixture=' + cookie + '; Path=/')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/':
                with mutex:
                    state['home'] += 1
                self.send('<a href="/xxl-job-admin/toLogin">登录</a>')
            elif self.path.endswith('.js'):
                with mutex:
                    state['scripts'] += 1
                self.send(SCRIPT, 'application/javascript')
            else:
                with mutex:
                    state['pages'] += 1
                token = secrets.token_hex(12)
                html = HTML.replace('</form>', '<input type="hidden" name="csrf" value="' + token + '"></form>')
                self.send(html + '<div>newdedecms</div>', cookie=token)

        def do_POST(self):
            data = {k: v[0] for k, v in parse_qs(self.rfile.read(
                int(self.headers['Content-Length'])).decode()).items()}
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            valid = 'fixture' in cookies and cookies['fixture'].value == data.get('csrf')
            with mutex:
                state['posts'].append((self.path, data))
                state['invalid_tokens'] += not valid
            success = valid and data.get('password') == 'correct-fixture'
            self.send(json.dumps({'code': 200 if success else 500, 'msg': 'ok' if success else '密码错误'},
                                 ensure_ascii=False), 'application/json')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class RecognitionReuseTests(unittest.TestCase):
    def test_jquery_candidates_refresh_tokens_without_rediscovery(self):
        transcript = io.StringIO()
        with jquery_fixture() as (base, state), fixture_settings(3):
            with contextlib.redirect_stdout(transcript), prepared_task(CrackTask, base + '/xxl-job-admin/toLogin') as task:
                self.assertEqual(task.crack_task(['fixture'], ['bad-' + str(i) for i in range(6)]),
                                 (False, False))
            self.assertEqual(state['scripts'], 1)
            self.assertEqual(state['invalid_tokens'], 0)
            candidates = [data for path, data in state['posts'] if data['password'].startswith('bad-')]
            self.assertEqual(len(candidates), 6)
            self.assertEqual(len({data['csrf'] for data in candidates}), 6)
            self.assertTrue(all(path == '/xxl-job-admin/login' for path, data in state['posts']))
        self.assertEqual(transcript.getvalue().count('识别 jQuery 表单登录接口:'), 1)
        self.assertEqual(transcript.getvalue().count('识别到cms:'), 1)

    def test_discovered_entry_and_recheck_reuse_identification(self):
        transcript = io.StringIO()
        with jquery_fixture() as (base, state), fixture_settings(2):
            with contextlib.redirect_stdout(transcript), prepared_task(CrackTask, base + '/') as task:
                self.assertEqual(task.crack_task(['fixture'], ['correct-fixture']),
                                 ('fixture', 'correct-fixture'))
            self.assertEqual(state['home'], 1)
            self.assertEqual(state['scripts'], 1)
            self.assertEqual(state['invalid_tokens'], 0)
            self.assertEqual(sum(data['password'] == 'correct-fixture' for _, data in state['posts']), 2)
        for message in ('发现同源登录页面:', '识别 jQuery 表单登录接口:', '识别到cms:'):
            self.assertEqual(transcript.getvalue().count(message), 1, message)

    def test_form_structure_change_reidentifies_without_stale_rules(self):
        template = Parser('http://example.test/login')
        template.response_url = template.url
        template.resp_content = '<form action="/old"><input name="username"><input type="password" name="password"></form>'
        template.form_parser()
        template.param_parser()
        template.post_path_parser()
        template.json_response_success = {'code': [200]}
        template.cms = {'name': 'old', 'success_flag': 'OLD_MARKER'}
        template.diagnostic_code = 'FORM_FOUND'
        changed = template.resp_content.replace('/old', '/new')

        def get_page(parser):
            parser.response_url = parser.url
            parser.resp_content = changed

        refreshed = Parser(template.url)
        with patch.object(Parser, 'get_resp_content', get_page), fixture_settings(2):
            self.assertTrue(refreshed.refresh_from(template))
        self.assertEqual(refreshed.post_path, 'http://example.test/new')
        self.assertEqual(refreshed.json_response_success, {})
        self.assertEqual(refreshed.cms, '')

    def test_new_captcha_blocks_refresh(self):
        template = Parser('http://example.test/login')
        template.response_url = template.url
        template.resp_content = '<form><input name="username"><input type="password" name="password"></form>'
        template.form_parser()
        template.param_parser()
        template.post_path_parser()
        template.diagnostic_code = 'FORM_FOUND'

        def get_page(parser):
            parser.response_url = parser.url
            parser.resp_content = template.resp_content.replace('</form>', '验证码</form>')

        refreshed = Parser(template.url)
        with patch.object(Parser, 'get_resp_content', get_page), fixture_settings(2):
            self.assertFalse(refreshed.refresh_from(template))
        self.assertEqual(refreshed.diagnostic_code, 'CAPTCHA_REQUIRED')

    def test_profile_refresh_does_not_repeat_profile_identification(self):
        profile = {'page_url': 'http://example.test/login', 'endpoint': '/session',
                   'username_field': 'username', 'password_field': 'password',
                   'success_fields': {'authenticated': True}}
        transcript = io.StringIO()
        with patch.dict(parserConfig, {'site_profiles': [profile]}), contextlib.redirect_stdout(transcript):
            template = Parser(profile['page_url'])
            self.assertTrue(template.run())
            with patch.object(Parser, 'get_resp_content') as get:
                refreshed = Parser(profile['page_url'])
                self.assertTrue(refreshed.refresh_from(template))
                get.assert_not_called()
        self.assertEqual(transcript.getvalue().count('使用配置式接口:'), 1)
        self.assertEqual(refreshed.post_path, 'http://example.test/session')

    def test_cli_deduplicates_exact_urls_preserving_first_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.txt'
            source.write_text('http://example.test/a\nhttp://example.test/b\nhttp://example.test/a\n')
            with patch.object(webcrack, 'multi_thread_crack', return_value=[]) as run, \
                    patch.object(webcrack, 'write_results'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(webcrack.main(['-f', str(source), '-o', directory + '/out']), 0)
            run.assert_called_once_with(['http://example.test/a', 'http://example.test/b'], threads=5)


if __name__ == '__main__':
    unittest.main()
