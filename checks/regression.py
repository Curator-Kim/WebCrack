"""离线回归：python3 -m unittest discover -s checks -p regression.py -v"""
import contextlib
import io
import unittest
import csv
import tempfile
from pathlib import Path
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
        with patch.object(webcrack, 'CrackTask') as task, patch.object(webcrack, 'write_results'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webcrack.main(['-u', 'https://example.test/', '--timeout', '5',
                                          '--delay', '0.2', '--proxy', 'http://localhost:8080',
                                          '--no-random-headers']), 0)
            task.return_value.run.assert_called_once_with(1, 'https://example.test/')
        self.assertEqual(config.crackConfig['timeout'], 5)
        self.assertEqual(config.crackConfig['delay'], .2)
        self.assertFalse(config.generatorConfig['headers_config']['enable'])

    def test_default_output_and_empty_results(self):
        with tempfile.TemporaryDirectory() as directory:
            default = Path(directory) / 'output.txt'
            with patch.dict(config.logConfig, {'output_filename': str(default)}), \
                    patch.object(webcrack, 'CrackTask') as task, \
                    contextlib.redirect_stdout(io.StringIO()):
                task.return_value.run.return_value = None
                self.assertEqual(webcrack.main(['-u', 'https://example.test/']), 0)
            self.assertEqual(default.read_text(), 'url\tusername\tpassword\n')

    def test_batch_output_only_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'urls.txt'
            source.write_text('https://example.test/a\nhttps://example.test/b\n')
            output = Path(directory) / 'nested' / 'success.txt'
            success = {'url': 'https://example.test/b', 'username': '测试', 'password': 'p\tq'}
            with patch.object(webcrack, 'CrackTask') as task, contextlib.redirect_stdout(io.StringIO()):
                task.return_value.run.side_effect = [None, success]
                self.assertEqual(webcrack.main(['-f', str(source), '-o', str(output)]), 0)
            with output.open(newline='') as handle:
                self.assertEqual(list(csv.DictReader(handle, delimiter='\t')), [success])
            webcrack.write_results(output, [])
            self.assertEqual(output.read_text(), 'url\tusername\tpassword\n')

    def test_recheck_controls_result(self):
        from crack.crack_task import CrackTask
        for verified in (True, False):
            with patch('crack.crack_task.Parser') as parser, \
                    patch.object(CrackTask, 'get_error_length', return_value=10), \
                    patch.object(CrackTask, 'crack_task', return_value=('user', 'pass') if verified else (False, False)), \
                    patch.object(CrackTask, 'recheck', return_value=verified), \
                    patch('crack.crack_task.Log'), contextlib.redirect_stdout(io.StringIO()):
                parser.return_value.run.return_value = True
                result = CrackTask().run(1, 'https://example.test/')
                expected = {'url': 'https://example.test/', 'username': 'user', 'password': 'pass'}
                self.assertEqual(result, expected if verified else None)

    def test_invalid_cli(self):
        for args in (['-u', 'a', '-f', 'b'], ['--timeout', '0'], ['--delay', '-1'],
                     ['--timeout', 'nan'], ['--proxy', 'localhost:8080']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    webcrack.main(args)
                self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
