"""超时重试次数、响应体读取、请求路径及停止事件回归。"""
import contextlib
import io
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from conf.config import crackConfig, parserConfig
from crack.crack_task import CrackTask
from http_requests import TaskStopped, request_with_timeout_retries
from parse.parser import Parser
from parse.resources import load_scripts
from checks.login_regression import response
from checks.request_concurrency_regression import fixture_settings


class TimeoutRetryTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(crackConfig, {'timeout_retries': 3, 'timeout_retry_delay': 0})
        self.settings.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.settings.stop()

    def test_success_after_three_timeouts(self):
        send = Mock(side_effect=[requests.ConnectTimeout(), requests.ReadTimeout(), requests.Timeout(), 'ok'])
        self.assertEqual(request_with_timeout_retries(send), 'ok')
        self.assertEqual(send.call_count, 4)

    def test_exhausted_retries_raise_last_timeout(self):
        error = requests.ReadTimeout('fixture')
        send = Mock(side_effect=error)
        with self.assertRaises(requests.ReadTimeout) as raised:
            request_with_timeout_retries(send)
        self.assertIs(raised.exception, error)
        self.assertEqual(send.call_count, 4)

    def test_wrapped_body_read_timeout_is_retried(self):
        error = requests.ConnectionError(ReadTimeoutError(None, '/script.js', 'fixture'))
        send = Mock(side_effect=[error, 'ok'])
        self.assertEqual(request_with_timeout_retries(send), 'ok')
        self.assertEqual(send.call_count, 2)

    def test_non_timeout_errors_not_retried(self):
        for error in (requests.ConnectionError('refused'), requests.exceptions.SSLError('TLS'),
                      requests.HTTPError('HTTP 500')):
            with self.subTest(error=type(error).__name__):
                send = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    request_with_timeout_retries(send)
                self.assertEqual(send.call_count, 1)

    def test_http_429_response_is_not_retried(self):
        send = Mock(return_value=response('limited', 429))
        self.assertEqual(request_with_timeout_retries(send).status_code, 429)
        self.assertEqual(send.call_count, 1)

    def test_stop_during_timeout_prevents_retry(self):
        stop = threading.Event()
        def send():
            stop.set()
            raise requests.ReadTimeout()
        send = Mock(side_effect=send)
        with self.assertRaises(TaskStopped) as raised:
            request_with_timeout_retries(send, stop_event=stop)
        # 取消仍属于 RequestException，既有宽泛处理不变，但可被精确识别。
        self.assertIsInstance(raised.exception, requests.RequestException)
        self.assertEqual(send.call_count, 1)

    def test_stop_before_request_raises_task_stopped(self):
        stop = threading.Event()
        stop.set()
        send = Mock(return_value='never')
        with self.assertRaises(TaskStopped):
            request_with_timeout_retries(send, stop_event=stop)
        send.assert_not_called()

    def test_non_timeout_errors_are_not_cancellation(self):
        stop = threading.Event()
        error = requests.ConnectionError('fixture')
        with self.assertRaises(requests.ConnectionError):
            request_with_timeout_retries(Mock(side_effect=error), stop_event=stop)
        self.assertIsInstance(error, requests.RequestException)

    def test_stop_interrupts_retry_wait(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        send = Mock(side_effect=requests.ReadTimeout())
        with self.assertRaises(TaskStopped):
            request_with_timeout_retries(send, stop_event=stop)
        self.assertEqual(send.call_count, 1)
        stop.wait.assert_called_once_with(0)

    def test_disabled_retries_and_invalid_configuration(self):
        send = Mock(side_effect=requests.Timeout())
        with patch.dict(crackConfig, {'timeout_retries': 0}), self.assertRaises(requests.Timeout):
            request_with_timeout_retries(send)
        self.assertEqual(send.call_count, 1)
        for settings in ({'timeout_retries': -1}, {'timeout_retries': True},
                         {'timeout_retry_delay': float('nan')}, {'timeout_retry_delay': -1}):
            with self.subTest(settings=settings), patch.dict(crackConfig, settings), self.assertRaises(ValueError):
                request_with_timeout_retries(Mock())

    def test_attempt_cancellation_is_not_a_request_failure(self):
        task = CrackTask()
        task.id, task.url = 1, 'https://example.test/login'
        stop = threading.Event()
        parser = SimpleNamespace(cms={}, username_keyword='u', password_keyword='p',
                                 post_path='https://example.test/session', request_format='form',
                                 captcha_required=False, data={})

        @contextlib.contextmanager
        def request_context(self, stop=None):
            yield Mock(), parser

        def cancel(*args, **kwargs):
            # 模拟另一候选在本次请求发起后置位停止事件
            stop.set()
            raise TaskStopped('任务已停止，取消请求及重试')

        with patch.object(CrackTask, '_request_context', request_context), \
                patch.object(CrackTask, 'crack_request', side_effect=cancel), \
                contextlib.redirect_stdout(io.StringIO()) as transcript:
            outcome = task._attempt('admin', 'secret', stop)
        self.assertIsNone(outcome)
        self.assertNotIn('登录请求异常', transcript.getvalue())

    def test_run_reports_cancellation_without_error(self):
        task = CrackTask()
        with patch('crack.crack_task.Parser') as parser, \
                patch.object(CrackTask, 'get_error_length', side_effect=TaskStopped('任务已停止，取消请求及重试')), \
                contextlib.redirect_stdout(io.StringIO()) as transcript:
            parser.return_value.run.return_value = True
            self.assertIsNone(task.run(1, 'https://example.test/login'))
            output = transcript.getvalue()
        self.assertIn('任务已停止，跳过剩余请求', output)
        self.assertNotIn('登录请求异常', output)
        self.assertNotIn('Parse Error', output)

    def test_login_post_keeps_payload_and_retries_three_times(self):
        task = CrackTask()
        task.parser = SimpleNamespace(data={'csrf': 'fixture'}, username_keyword='username',
                                      password_keyword='password', post_path='http://example.test/session',
                                      request_format='json', request_headers={})
        conn = Mock()
        conn.post.side_effect = [requests.Timeout(), requests.Timeout(), requests.Timeout(), response('密码错误')]
        with patch('crack.crack_task.time.sleep'):
            task.crack_request(conn, 'fixture', 'fixture-password')
        self.assertEqual(conn.post.call_count, 4)
        for call in conn.post.call_args_list:
            self.assertEqual(call.kwargs['json'], {'csrf': 'fixture', 'username': 'fixture', 'password': 'fixture-password'})
            self.assertEqual(call.kwargs['timeout'], task.timeout)
        self.assertEqual(task.parser.data, {'csrf': 'fixture'})

    def test_page_and_discovered_entry_get_retry(self):
        parser = Parser('http://example.test/')
        parser.session = Mock()
        parser.session.get.side_effect = [requests.Timeout(), requests.Timeout(), requests.Timeout(), response('page')]
        parser.get_resp_content()
        self.assertEqual(parser.session.get.call_count, 4)
        parser.response_url = parser.url
        parser.resp_content = '<a href="/login">登录</a>'
        parser.session.get.reset_mock()
        parser.session.get.side_effect = [requests.ReadTimeout(), response('login')]
        self.assertTrue(parser.discover_login_entry())
        self.assertEqual(parser.session.get.call_count, 2)
        self.assertFalse(parser.session.get.call_args.kwargs['allow_redirects'])

    def test_js_body_timeout_discards_partial_content_and_closes_response(self):
        parser = Parser('http://example.test/')
        parser.response_url = parser.url
        parser.resp_content = '<script src="/app.js"></script>'
        parser.session = Mock()
        closed = []
        class Stream:
            status_code = 200
            def __init__(self, fail): self.fail = fail
            def __enter__(self): return self
            def __exit__(self, *args): closed.append(self.fail)
            def iter_content(self, size):
                if self.fail:
                    yield b'PARTIAL'
                    raise requests.ConnectionError(ReadTimeoutError(None, '/app.js', 'fixture'))
                yield b'COMPLETE'
        parser.session.get.side_effect = [Stream(True), Stream(False)]
        self.assertEqual(load_scripts(parser, parserConfig), ['COMPLETE'])
        self.assertEqual(closed, [True, False])
        self.assertEqual(parser.script_cache['http://example.test/app.js'], 'COMPLETE')
        self.assertEqual(parser.session.get.call_count, 2)

    def test_js_exhaustion_preserves_warning_and_no_partial_cache(self):
        parser = Parser('http://example.test/')
        parser.response_url = parser.url
        parser.resp_content = '<script src="/app.js"></script>'
        parser.session = Mock()
        parser.session.get.side_effect = requests.ReadTimeout('fixture')
        self.assertEqual(load_scripts(parser, parserConfig), [])
        self.assertEqual(parser.session.get.call_count, 4)
        self.assertIsNone(parser.script_cache['http://example.test/app.js'])
        self.assertIn('ReadTimeout', parser.resource_warnings[0])

    def test_real_stream_timeout_retries_and_recovers(self):
        state = {'gets': 0}
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                with lock:
                    state['gets'] += 1
                    number = state['gets']
                body = b'COMPLETE'
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if number <= 3:
                    time.sleep(.1)  # 请求已收到响应头，响应体读取发生真实超时。
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            with fixture_settings(1), requests.Session() as session:
                parser = Parser('http://127.0.0.1:' + str(server.server_port) + '/')
                parser.response_url = parser.url
                parser.timeout = .03
                parser.session = session
                parser.resp_content = '<script src="/app.js"></script>'
                self.assertEqual(load_scripts(parser, parserConfig), ['COMPLETE'])
                self.assertEqual(state['gets'], 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
