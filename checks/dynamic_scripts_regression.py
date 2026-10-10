"""动态 script 声明的有限静态提取；离线及 loopback 回归。"""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch
import requests

from conf.config import parserConfig
from parse.parser import Parser
from parse.resources import dynamic_script_paths, load_scripts
from checks.axios_regression import BUNDLE


class DynamicScriptTests(unittest.TestCase):
    def test_literal_constants_and_loop_version_wrapper(self):
        scripts = ["const base='./assets/'; const paths=[base+'app.js','./assets/login.js'];",
                   "paths.forEach(src=>{const script=document.createElement('script');"
                   "script.src=window.utils.getVersionedUrl(src);document.body.appendChild(script);});"]
        self.assertEqual(dynamic_script_paths(scripts), ['./assets/login.js', './assets/app.js'])
        self.assertEqual(dynamic_script_paths(["const s=document.createElement('script');s.src='/app.js?v=2';"]),
                         ['/app.js?v=2'])

    def test_function_callback_and_template_constant(self):
        code = "const base='/assets';const scripts=[`${base}/app.js`];scripts.forEach(function(src){var s=document.createElement('script');s.src=src;});"
        self.assertEqual(dynamic_script_paths([code]), ['/assets/app.js'])

    def test_non_script_elements_and_unknown_expressions_ignored(self):
        code = "const image=document.createElement('img');image.src='/fake.js';const files=['/unrelated.js'];const s=document.createElement('script');s.src=decrypt('/encrypted.js');s.src=remoteResponse.path;"
        self.assertEqual(dynamic_script_paths([code]), [])

    def test_comments_and_strings_are_not_executed(self):
        code = "//const s=document.createElement('script');s.src='/comment.js';\nconst text=\"const s=document.createElement('script');s.src='/string.js';\";"
        self.assertEqual(dynamic_script_paths([code]), [])

    def test_ambiguous_array_and_wrong_assignment_are_ignored(self):
        code = "const paths=['/one.js'];const paths=['/two.js'];paths.forEach(src=>{const s=document.createElement('script');s.src=src;});"
        self.assertEqual(dynamic_script_paths([code]), [])

    def test_main_scripts_prioritized_over_locales(self):
        code = "const languages=['/i18n/a.js','/i18n/b.js'];languages.forEach(src=>{const s=document.createElement('script');s.src=src;});const scripts=['/assets/vendors.js','/assets/umi.js'];scripts.forEach(src=>{const s=document.createElement('script');s.src=src;});"
        self.assertEqual(dynamic_script_paths([code], 2), ['/assets/umi.js', '/assets/vendors.js'])
        self.assertEqual(dynamic_script_paths([code], 0), [])

    def resources(self, html, sources, **settings):
        parser = Parser('https://example.test/page')
        parser.response_url = parser.url
        parser.resp_content = html
        requested = []

        class Stream:
            status_code = 200
            def __init__(self, url): self.url = url
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield sources[self.url].encode()

        def get(url, **kwargs):
            requested.append(url)
            self.assertFalse(kwargs['allow_redirects'])
            return Stream(url)

        parser.session = Mock()
        parser.session.get.side_effect = get
        config = dict(parserConfig, **settings)
        scripts = load_scripts(parser, config)
        return parser, requested, scripts, config

    def test_loader_same_origin_duplicates_cache_and_disable(self):
        html = "<script>const paths=['/login.js','/login.js','https://other.test/evil.js'];paths.forEach(src=>{const s=document.createElement('script');s.src=src;});</script>"
        source = {'https://example.test/login.js': '/* fixture */'}
        parser, requested, scripts, config = self.resources(html, source)
        self.assertEqual(requested, ['https://example.test/login.js'])
        load_scripts(parser, config)
        self.assertEqual(requested, ['https://example.test/login.js'])
        _, requested, _, _ = self.resources(html, source, discover_dynamic_scripts=False)
        self.assertEqual(requested, [])

    def test_dynamic_dependencies_are_depth_bounded(self):
        html = '<script src="/entry.js"></script>'
        sources = {'https://example.test/entry.js': "const s=document.createElement('script');s.src='./next.js';",
                   'https://example.test/next.js': "const s=document.createElement('script');s.src='./deep.js';"}
        _, requested, _, _ = self.resources(html, sources, script_dependency_depth=1)
        self.assertEqual(requested, ['https://example.test/entry.js', 'https://example.test/next.js'])
        _, requested, _, _ = self.resources(html, sources, script_dependency_depth=0)
        self.assertEqual(requested, ['https://example.test/entry.js'])

    def test_resource_count_and_file_size_limits_still_apply(self):
        html = "<script>const paths=['/a.js','/b.js'];paths.forEach(src=>{const s=document.createElement('script');s.src=src;});</script>"
        sources = {'https://example.test/a.js': 'x' * 1000, 'https://example.test/b.js': 'fixture'}
        _, requested, scripts, _ = self.resources(html, sources, json_script_limit=1, json_script_max_bytes=512)
        self.assertEqual(requested, ['https://example.test/a.js'])
        self.assertNotIn('x' * 1000, scripts)

    def test_real_http_dynamic_script_discovery_uses_get_only(self):
        from checks.request_concurrency_regression import axios_fixture, fixture_settings
        html = "<div id='root'></div><script>const files=['/assets/app.js'];files.forEach(src=>{const s=document.createElement('script');s.src=src;});</script>"
        with patch('checks.axios_regression.SHELL', html), axios_fixture() as (url, state), fixture_settings(2):
            with requests.Session() as session:
                parser = Parser(url, session=session)
                self.assertTrue(parser.run())
                self.assertEqual(parser.post_path, url.rsplit('/', 1)[0] + '/api/auth/login')
            self.assertEqual(state['scripts'], 1)
            self.assertEqual(state['posts'], 0)

    def test_parser_discovers_axios_from_dynamic_script_without_post(self):
        html = "<div id='root'></div><script>const scripts=['/assets/app.js'];scripts.forEach(src=>{const s=document.createElement('script');s.src=src;});</script>"
        sources = {'https://example.test/assets/app.js': BUNDLE}
        parser, requested, _, _ = self.resources(html, sources)
        def get_page():
            parser.resp_content = html
            parser.response_url = parser.url
        with patch.object(parser, 'get_resp_content', get_page), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(parser.run())
        self.assertEqual(parser.post_path, 'https://example.test/api/auth/login')
        parser.session.post.assert_not_called()
        self.assertEqual(requested, ['https://example.test/assets/app.js'])


if __name__ == '__main__':
    unittest.main()
