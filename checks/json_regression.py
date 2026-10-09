import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from conf.config import parserConfig, crackConfig
from parse.parser import Parser, ParseIssue
from crack.crack_task import CrackTask, LoginState
from checks.login_regression import response


class JsonTests(unittest.TestCase):
    def parser(self, script):
        parser = Parser('https://example.test/login')
        parser.response_url = parser.url
        parser.resp_content = '<script>' + script + '</script>'
        return parser

    def test_shorthand_and_mapping(self):
        for fields in ('username,password', 'username: login.value,password: secret.value'):
            parser = self.parser("fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({" + fields + "})})")
            self.assertTrue(parser.json_login_parser())
            self.assertEqual(parser.request_format, 'json')
            self.assertEqual(parser.post_path, 'https://example.test/api/login')

    def test_reject_cross_origin_and_ambiguous(self):
        for script in (
            "fetch('https://other.test/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})})",
            "fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});" +
            "fetch('/api/signin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})})"):
            if "other.test" in script:
                self.assertFalse(self.parser(script).json_login_parser())
            else:
                with self.assertRaises(ParseIssue) as raised:
                    self.parser(script).json_login_parser()
                self.assertEqual(raised.exception.code, "AMBIGUOUS_INTERFACE")

    def test_json_payload(self):
        task = CrackTask()
        task.parser = SimpleNamespace(data={}, username_keyword='username', password_keyword='password',
                                      request_format='json', post_path='https://example.test/api/login')
        conn = Mock()
        conn.post.return_value = response('{"token":"fixture"}', content_type='application/json')
        with patch('crack.crack_task.time.sleep'):
            task.crack_request(conn, 'fixture', 'fixture')
        kwargs = conn.post.call_args.kwargs
        self.assertEqual(kwargs['json'], {'username': 'fixture', 'password': 'fixture'})
        self.assertNotIn('data', kwargs)
        self.assertEqual(kwargs['headers']['Content-Type'], 'application/json')

    def test_token_validation(self):
        task = CrackTask()
        task.parser = SimpleNamespace(cms={}, username_keyword='username', password_keyword='password', request_format='json')
        with patch.dict(crackConfig, {'json_success_fields': {}, 'success_words': []}):
            for body, expected in (('{"token":"fixture"}', LoginState.SUCCESS), ('{"token":""}', LoginState.UNKNOWN),
                                   ('{"token":true}', LoginState.UNKNOWN), ('{"token":null}', LoginState.UNKNOWN)):
                self.assertEqual(task.classify_response(response(body, content_type='application/json')), expected)
            task.baseline_responses = [response('{"token":"public"}', content_type='application/json')]
            self.assertEqual(task.classify_response(response('{"token":"fixture"}', content_type='application/json')), LoginState.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
