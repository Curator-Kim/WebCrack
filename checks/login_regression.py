"""Response fixtures only: no network traffic."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from crack.crack_task import CrackTask, LoginState
from conf.config import crackConfig


def response(body, status=200, content_type='text/html'):
    res = requests.Response()
    res.status_code = status
    res._content = body.encode()
    res.encoding = 'utf-8'
    res.url = 'https://example.test/dashboard'
    res.headers['Content-Type'] = content_type
    return res


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(crackConfig, {'success_words': ['AUTHENTICATED'],
                                               'json_success_fields': {}, 'concurrency': 1})
        self.settings.start()
        self.task = CrackTask()
        self.task.parser = SimpleNamespace(cms={}, username_keyword='username', password_keyword='password',
                                           data={'username': '', 'password': '', 'token': 'fixture'},
                                           post_path='https://example.test/login')
        self.task.baseline_responses = [response('invalid credentials')]

    def tearDown(self):
        self.settings.stop()

    def test_length_change_is_unknown(self):
        self.assertEqual(self.task.classify_response(response('unexpected page with a different length')), LoginState.UNKNOWN)

    def test_equal_length_success(self):
        self.task.baseline_responses = [response('X' * len('AUTHENTICATED'))]
        self.assertEqual(self.task.classify_response(response('AUTHENTICATED')), LoginState.SUCCESS)

    def test_http_errors_and_redirects(self):
        for code in (302, 403, 404, 500, 503):
            with self.subTest(code=code):
                self.assertEqual(self.task.classify_response(response('AUTHENTICATED', code)), LoginState.ERROR)
        self.assertEqual(self.task.classify_response(response('AUTHENTICATED', 401)), LoginState.FAILURE)
        self.assertEqual(self.task.classify_response(response('AUTHENTICATED', 429)), LoginState.STOPPED)

    def test_negative_evidence_overrides_success(self):
        for body in ('AUTHENTICATED 密码错误', 'AUTHENTICATED <form><input type="password"></form>',
                     'AUTHENTICATED <form><input name="username"><input name="password"></form>'):
            self.assertEqual(self.task.classify_response(response(body)), LoginState.FAILURE)
        self.assertEqual(self.task.classify_response(response('AUTHENTICATED 已被锁定')), LoginState.STOPPED)

    def test_baseline_marker_not_success(self):
        self.task.baseline_responses = [response('public AUTHENTICATED banner')]
        self.assertEqual(self.task.classify_response(response('AUTHENTICATED')), LoginState.UNKNOWN)

    def test_json_rule_and_strict_types(self):
        with patch.dict(crackConfig, {'success_words': [], 'json_success_fields': {'authenticated': True}}):
            self.assertEqual(self.task.classify_response(response(json.dumps({'authenticated': True}), content_type='application/json')), LoginState.SUCCESS)
            for body in ('{"authenticated": false}', '{"authenticated": 1}', '{"authenticated": "true"}', 'broken', '{}'):
                self.assertEqual(self.task.classify_response(response(body, content_type='application/json')), LoginState.UNKNOWN)

    def test_dynamic_baseline_does_not_abort(self):
        with patch.object(self.task, 'crack_request', side_effect=[response('bad'), response('bad with a timestamp')]):
            self.task.get_error_length()
        self.assertEqual(len(self.task.baseline_responses), 2)
        self.task.conn.close()

    def test_recheck_uses_same_rules(self):
        for candidate, expected in ((response('different page'), False), (response('AUTHENTICATED'), True),
                                    (response('AUTHENTICATED', 500), False)):
            self.task.stopped = False
            with patch.object(self.task, '_fresh_response', side_effect=[response('bad'), candidate]):
                self.assertEqual(self.task.recheck('fixture', 'fixture'), expected)

    def test_stop_no_more_requests(self):
        with patch.object(self.task, 'crack_request', return_value=response('limited', 429)) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.task.crack_task(['fixture'], ['a', 'b']), (False, False))
        self.assertEqual(send.call_count, 1)
        self.assertTrue(self.task.stopped)

    def test_failed_recheck_continues(self):
        with patch.object(self.task, 'crack_request', return_value=response('AUTHENTICATED')), \
                patch.object(self.task, 'recheck', side_effect=[False, True]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.task.crack_task(['fixture'], ['a', 'b']), ('fixture', 'b'))

    def test_request_preserves_form(self):
        with patch('crack.crack_task.time.sleep'):
            from unittest.mock import Mock
            conn = Mock()
            conn.post.return_value = response('bad')
            self.task.crack_request(conn, 'fixture', 'fixture')
            self.assertEqual(self.task.parser.data['username'], '')

    def test_fresh_session_and_parser_restored(self):
        previous = self.task.parser
        with patch('crack.crack_task.requests.session') as session, \
                patch('crack.crack_task.Parser') as parser, \
                patch.object(self.task, 'crack_request', return_value=response('bad')):
            parser.return_value.run.return_value = True
            self.task._fresh_response('fixture', 'fixture')
            parser.assert_called_once_with(self.task.url, session=session.return_value.__enter__.return_value)
            session.return_value.__exit__.assert_called_once()
        self.assertIs(self.task.parser, previous)


if __name__ == '__main__':
    unittest.main()
