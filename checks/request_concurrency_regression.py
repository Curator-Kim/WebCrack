"""请求粒度并发回归：模拟调度器及真实 loopback HTTP。"""
import contextlib
import io
import json
import os
import secrets
import threading
import time
import unittest
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

import requests
import webcrack
from conf.config import crackConfig, generatorConfig
from crack.crack_task import CrackTask, LoginState
from parse.parser import Parser


@contextmanager
def login_fixture(delay=.04):
    mutex = threading.Lock()
    state = {'active': 0, 'peak': 0, 'requests': [], 'invalid_tokens': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, cookie=None):
            raw = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            if cookie:
                self.send_header('Set-Cookie', 'fixture=' + cookie + '; Path=/')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            token = secrets.token_hex(12)
            self.send('<form method="post" action="/login"><input name="username">'
                      '<input name="password" type="password"><input name="csrf" '
                      'type="hidden" value="' + token + '"></form>', token)

        def do_POST(self):
            data = {key: values[0] for key, values in parse_qs(
                self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            valid_token = 'fixture' in cookie and cookie['fixture'].value == data.get('csrf')
            with mutex:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['requests'].append(data)
                state['invalid_tokens'] += not valid_token
            try:
                time.sleep(delay)
                valid = valid_token and data.get('password') == 'correct-fixture'
                self.send('AUTHENTICATED' if valid else '密码错误')
            finally:
                with mutex:
                    state['active'] -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/login', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def fixture_settings(concurrency):
    with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}), \
            patch.dict(crackConfig, {'concurrency': concurrency, 'success_words': ['AUTHENTICATED'],
                                 'delay': 0, 'requests_proxies': {}, 'timeout': 3}), \
            patch.dict(generatorConfig['headers_config'], {'enable': False}), \
            contextlib.redirect_stdout(io.StringIO()):
        yield


@contextmanager
def prepared_task(task_class, url):
    task = task_class()
    task.url = url
    with requests.Session() as session:
        task.conn = session
        task.parser = Parser(url, session=session)
        assert task.parser.run()
        task.get_error_length()
        yield task


@contextmanager
def axios_fixture(delay=.025, script_delay=.04):
    from checks.axios_regression import BUNDLE, SHELL
    mutex = threading.Lock()
    state = {'pages': 0, 'scripts': 0, 'active': 0, 'peak': 0, 'cookies': set(),
             'posts': 0, 'clients': set()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def send(self, body, kind, cookie=None):
            raw = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            if cookie:
                self.send_header('Set-Cookie', 'fixture=' + cookie + '; Path=/')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            script = self.path.endswith('.js')
            with mutex:
                state['scripts' if script else 'pages'] += 1
            if script:
                time.sleep(script_delay)
                self.send('/*' + 'x' * 300000 + '*/' + BUNDLE, 'application/javascript')
            else:
                self.send(SHELL, 'text/html', secrets.token_hex(12))

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with mutex:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['posts'] += 1
                state['cookies'].add(self.headers.get('Cookie'))
                state['clients'].add(self.client_address)
            try:
                time.sleep(delay)
                self.send(json.dumps({'code': 500, 'data': None, 'message': '密码错误'},
                                     ensure_ascii=False), 'application/json')
            finally:
                with mutex:
                    state['active'] -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/login', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class RequestConcurrencyTests(unittest.TestCase):
    def test_axios_discovery_once_and_connections_reused(self):
        transcript = io.StringIO()
        with axios_fixture() as (url, state), fixture_settings(3):
            with contextlib.redirect_stdout(transcript), prepared_task(CrackTask, url) as task:
                self.assertEqual(task.crack_task(['fixture'], ['bad-' + str(i) for i in range(6)]),
                                 (False, False))
                self.assertIsNone(task._json_sessions)
            self.assertEqual(state['scripts'], 1)
            self.assertLessEqual(state['pages'], 4)  # 初始识别 + 每线程一次 Cookie 初始化。
            self.assertEqual(state['posts'], 8)  # 两个基线 + 六个候选。
            self.assertEqual(state['peak'], 3)
            self.assertNotIn(None, state['cookies'])
            self.assertLessEqual(len(state['cookies']), 4)
            self.assertLessEqual(len(state['clients']), 4)
        self.assertEqual(transcript.getvalue().count('识别 Axios JSON 登录接口:'), 1)

    def test_copied_json_plan_has_independent_mutable_fields(self):
        with fixture_settings(3):
            task = CrackTask()
            task.parser = SimpleNamespace(data={'nested': {'value': 1}}, request_headers={'X': 'fixture'},
                                          json_response_success={'code': [200]}, cms={})
            parser = task._copy_request_plan(None)
            parser.data['nested']['value'] = 2
            parser.request_headers['X'] = 'changed'
            parser.json_response_success['code'].append(201)
            self.assertEqual(task.parser.data['nested']['value'], 1)
            self.assertEqual(task.parser.request_headers['X'], 'fixture')
            self.assertEqual(task.parser.json_response_success['code'], [200])

    def test_real_posts_overlap_and_cookie_tokens_are_isolated(self):
        with login_fixture() as (url, state), fixture_settings(3):
            with prepared_task(CrackTask, url) as task:
                self.assertEqual(task.crack_task(['fixture'], ['bad-' + str(i) for i in range(6)]),
                                 (False, False))
            self.assertEqual(state['peak'], 3)
            self.assertEqual(state['invalid_tokens'], 0)
            attempts = [r for r in state['requests'] if r['password'].startswith('bad-')]
            self.assertEqual(len(attempts), 6)
            self.assertEqual(len({r['csrf'] for r in attempts}), 6)
            self.assertEqual(state['active'], 0)

    def test_real_candidate_success_is_rechecked(self):
        with login_fixture() as (url, state), fixture_settings(2):
            with prepared_task(CrackTask, url) as task:
                self.assertEqual(task.crack_task(['fixture'], ['bad-1', 'correct-fixture', 'bad-2']),
                                 ('fixture', 'correct-fixture'))
            self.assertEqual(state['invalid_tokens'], 0)
            self.assertEqual(sum(r['password'] == 'correct-fixture' for r in state['requests']), 2)

    def test_serial_setting_has_one_inflight_post(self):
        with login_fixture() as (url, state), fixture_settings(1):
            with prepared_task(CrackTask, url) as task:
                self.assertEqual(task.crack_task(['fixture'], ['bad-1', 'bad-2']), (False, False))
            self.assertEqual(state['peak'], 1)
            self.assertEqual(state['invalid_tokens'], 0)

    def test_failed_recheck_continues_without_losing_candidates(self):
        with fixture_settings(2):
            task = CrackTask()
            worker = SimpleNamespace(stopped=False, recheck=Mock(side_effect=[False, True]))
            attempts = []
            barrier = threading.Barrier(2)

            def attempt(username, password, stop):
                attempts.append(password)
                barrier.wait(timeout=5)
                return LoginState.SUCCESS, username, password, worker

            with patch.object(task, '_attempt', side_effect=attempt):
                result = task.crack_task(['fixture'], ['a', 'b'])
            self.assertEqual(result[0], 'fixture')
            self.assertIn(result[1], ('a', 'b'))
            self.assertCountEqual(attempts, ['a', 'b'])
            self.assertEqual(worker.recheck.call_count, 2)

    def test_stop_response_prevents_refill(self):
        with fixture_settings(2):
            task = CrackTask()
            barrier = threading.Barrier(2)
            attempts = []

            def attempt(username, password, stop):
                attempts.append(password)
                barrier.wait(timeout=5)
                stop.set()
                return LoginState.STOPPED, username, password, None

            with patch.object(task, '_attempt', side_effect=attempt):
                self.assertEqual(task.crack_task(['fixture'], list('abcdef')), (False, False))
            self.assertTrue(task.stopped)
            self.assertCountEqual(attempts, ['a', 'b'])

    def test_verified_success_prevents_refill(self):
        with fixture_settings(2):
            task = CrackTask()
            barrier = threading.Barrier(2)
            attempts = []
            worker = SimpleNamespace(stopped=False, recheck=Mock(return_value=True))

            def attempt(username, password, stop):
                attempts.append(password)
                barrier.wait(timeout=5)
                return LoginState.SUCCESS, username, password, worker

            with patch.object(task, '_attempt', side_effect=attempt):
                self.assertEqual(task.crack_task(['fixture'], list('abcdef'))[0], 'fixture')
            self.assertCountEqual(attempts, ['a', 'b'])
            worker.recheck.assert_called_once()

    def test_attempt_exception_closes_session_and_signals_stop(self):
        with fixture_settings(2):
            task = CrackTask()
            stop = threading.Event()
            with patch('crack.crack_task.requests.session') as session, \
                    patch('crack.crack_task.Parser', side_effect=RuntimeError('fixture failure')), \
                    patch('crack.crack_task.Log.Error'):
                result = task._attempt('fixture', 'fixture', stop)
            self.assertEqual(result[0], LoginState.ERROR)
            self.assertTrue(stop.is_set())
            session.return_value.__exit__.assert_called_once()

    def test_concurrent_credential_500_is_skipped(self):
        with fixture_settings(2):
            task = CrackTask()
            attempts = []
            workers = {}

            def attempt(username, password, stop):
                attempts.append(password)
                worker = SimpleNamespace(stopped=False, recheck=Mock(return_value=True))
                if password == 'poison':
                    worker.server_error = True
                    worker.server_error_status = 500
                    return LoginState.ERROR, username, password, worker
                return LoginState.SUCCESS, username, password, worker

            with patch.object(task, '_attempt', side_effect=attempt):
                self.assertEqual(task.crack_task(['fixture'], ['poison', 'good'])[0], 'fixture')
            self.assertEqual(attempts, ['poison', 'good'])
            self.assertFalse(task.stopped)

    def test_concurrent_500_streak_stops_site(self):
        with fixture_settings(2), patch.dict(crackConfig, {'server_error_limit': 2}):
            task = CrackTask()

            def attempt(username, password, stop):
                worker = SimpleNamespace(stopped=False, server_error=True, server_error_status=500,
                                         recheck=Mock(return_value=True))
                return LoginState.ERROR, username, password, worker

            with patch.object(task, '_attempt', side_effect=attempt):
                self.assertEqual(task.crack_task(['fixture'], ['a', 'b', 'c']), (False, False))
            self.assertTrue(task.stopped)

    def test_empty_and_stopped_tasks_send_no_requests(self):
        with fixture_settings(2):
            task = CrackTask()
            with patch.object(task, '_attempt') as attempt:
                self.assertEqual(task.crack_task([], ['fixture']), (False, False))
                task.stopped = True
                self.assertEqual(task.crack_task(['fixture'], ['fixture']), (False, False))
                attempt.assert_not_called()

    def test_cli_concurrency_validation_and_override(self):
        for value in ('0', '-1', 'bad', '1.5'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                webcrack.main(['-u', 'fixture', '-c', value])
            self.assertEqual(error.exception.code, 2)
        with fixture_settings(2), patch.object(webcrack, 'multi_thread_crack', return_value=[]), \
                patch.object(webcrack, 'write_results'):
            self.assertEqual(webcrack.main(['-u', 'fixture', '-c', '3']), 0)
            self.assertEqual(CrackTask().concurrency, 3)


if __name__ == '__main__':
    unittest.main()
