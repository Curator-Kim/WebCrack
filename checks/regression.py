"""离线回归：python3 -m unittest discover -s checks -p regression.py -v"""
import contextlib
import io
import unittest
from unittest.mock import patch
from copy import deepcopy

from conf import config
from generator.header import get_random_headers
from parse.parser import Parser
import webcrack


class Regression(unittest.TestCase):
    def setUp(self):
        self.crack = deepcopy(config.crackConfig)
        self.headers = deepcopy(config.generatorConfig['headers_config'])

    def tearDown(self):
        config.crackConfig.clear()
        config.crackConfig.update(self.crack)
        config.generatorConfig['headers_config'].clear()
        config.generatorConfig['headers_config'].update(self.headers)

    def test_real_user_agent(self):
        config.generatorConfig['headers_config']['enable'] = True
        headers = get_random_headers()
        self.assertIn('Mozilla/', headers['User-Agent'])
        self.assertNotIn('X-Forwarded-For', headers)
        self.assertNotIn('Client-IP', headers)

    def test_disabled_and_independent_headers(self):
        config.generatorConfig['headers_config']['enable'] = False
        headers = get_random_headers()
        self.assertEqual(headers, self.headers['default_headers'])
        headers['User-Agent'] = 'changed'
        self.assertNotEqual(get_random_headers()['User-Agent'], 'changed')

    def test_valid_forms(self):
        for html in (
            '<form><input name="username"><input name="password" type="password"></form>',
            '<FORM\n ACTION="../auth"><INPUT NAME="username"><INPUT NAME="password" TYPE="PASSWORD"></FORM>',
            '<form id="search"><input name="q"></form><form id="login"><input name="username"><input name="password" type="password"></form>',
            '<form><input name="username"><input name="password"></form><form><input name="q"></form>',
        ):
            with self.subTest(html=html):
                parser = Parser('https://example.test/admin/login')
                parser.resp_content = html
                parser.form_parser()
                parser.check_login_page()
                parser.param_parser()
                self.assertEqual(parser.username_keyword, 'username')
                self.assertEqual(parser.password_keyword, 'password')

    def test_actions(self):
        for action, expected in (('', 'https://example.test/admin/login'),
                                 ('../auth', 'https://example.test/auth'),
                                 ('/auth', 'https://example.test/auth'),
                                 ('//example.test/auth', 'https://example.test/auth')):
            parser = Parser('https://example.test/old')
            parser.response_url = 'https://example.test/admin/login'
            parser.resp_content = f'<form action="{action}"></form>'
            parser.form_parser()
            parser.post_path_parser()
            self.assertEqual(parser.post_path, expected)

    def test_no_form(self):
        parser = Parser('https://example.test/')
        parser.resp_content = '<p>empty</p>'
        with self.assertRaises(ValueError):
            parser.form_parser()

    def test_cli_applies_configuration(self):
        with patch.object(webcrack, 'CrackTask') as task, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webcrack.main(['-u', 'https://example.test/', '--timeout', '5',
                                          '--delay', '0.2', '--proxy', 'http://localhost:8080',
                                          '--no-random-headers']), 0)
            task.return_value.run.assert_called_once_with(1, 'https://example.test/')
        self.assertEqual(config.crackConfig['timeout'], 5)
        self.assertEqual(config.crackConfig['delay'], .2)
        self.assertFalse(config.generatorConfig['headers_config']['enable'])

    def test_invalid_cli(self):
        for args in (['-u', 'a', '-f', 'b'], ['--timeout', '0'], ['--delay', '-1'],
                     ['--timeout', 'nan'], ['--proxy', 'localhost:8080']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    webcrack.main(args)
                self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
