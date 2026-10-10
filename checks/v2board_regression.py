"""V2Board adapter: offline negative cases and real loopback form POSTs."""
import contextlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests
from conf.config import crackConfig, parserConfig, generatorConfig
from parse.parser import Parser, ParseIssue
from crack.crack_task import CrackTask, LoginState
from checks.login_regression import response

BUNDLE = '''var c=!1,u=new URL(window.location.href).origin;
window.settings.host&&(u=window.settings.host);
var l={serviceHost:c?"http://localhost/api/v1":u+"/api/v1"};
function post(e,t){return fetch(e,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:encode(t)})}
Object(i["b"])("/passport/auth/login",{email:r,password:o});
Object(c["p"])(f.data.auth_data);
'''
CONFIG = "window.defaultConfig={apiUrl:'https://backend.example.test'};window.settings={title:'fixture'};"
INTERCEPTOR = "window.fetch=function(url,options){const urlObj=new URL(url,window.location.href);url=window.defaultConfig.apiUrl+urlObj.pathname+urlObj.search;return originalFetch(url,options)};"
SHELL = '''<div id="root"></div><script>window.settings={};const scripts=['/assets/umi.js'];scripts.forEach(src=>{const s=document.createElement('script');s.src=window.utils.getVersionedUrl(src);document.body.appendChild(s);});</script>'''


class V2BoardTests(unittest.TestCase):
    def test_default_configuration_has_no_site_exception(self):
        self.assertEqual(parserConfig['v2board_api_origins'], [])

    def test_minified_regex_does_not_hide_login_evidence(self):
        from parse.recognizers import mask
        prefix = r'''const rx=/["']/;const urlRx=/https?:\/\/[^/]+/gi;const ratio=left/right;'''
        source=prefix+BUNDLE.replace('\n','')
        masked=mask(source)
        self.assertEqual(len(masked),len(source))
        self.assertIn('.data.auth_data',masked)
        self.assertIn('left/right',masked)
        p=self.parser([source])
        self.assertEqual(p.post_path,'http://example.test/api/v1/passport/auth/login')

    def parser(self, sources=None):
        p = Parser('http://example.test/#/login')
        p.response_url = p.url
        p.resp_content = '<div id="root"></div>'
        with patch('parse.parser.load_scripts', return_value=sources or [BUNDLE]):
            self.assertTrue(p.json_login_parser())
        return p

    def test_endpoint_and_form_encoding(self):
        p = self.parser()
        self.assertEqual(p.post_path, 'http://example.test/api/v1/passport/auth/login')
        self.assertEqual((p.username_keyword, p.password_keyword, p.request_format), ('email', 'password', 'form'))
        task = CrackTask(); task.parser = p
        conn = Mock(); conn.post.return_value = response('{}', content_type='application/json')
        with patch('crack.crack_task.time.sleep'):
            task.crack_request(conn, 'fixture@example.test', 'fixturepass')
        self.assertEqual(conn.post.call_args.kwargs['data'], {'email':'fixture@example.test', 'password':'fixturepass'})
        self.assertNotIn('json', conn.post.call_args.kwargs)
        self.assertEqual(conn.post.call_args.kwargs['headers']['Accept'], 'application/json')

    def test_cross_origin_requires_explicit_origin_no_posts(self):
        p = Parser('http://example.test/'); p.response_url=p.url; p.resp_content=''
        with patch('parse.parser.load_scripts', return_value=[CONFIG,INTERCEPTOR,BUNDLE]):
            with self.assertRaises(ParseIssue) as raised:
                p.json_login_parser()
            self.assertEqual(raised.exception.code, 'V2BOARD_CROSS_ORIGIN')
            with patch.dict(parserConfig, {'v2board_api_origins':['https://backend.example.test']}):
                self.assertTrue(p.json_login_parser())
                self.assertEqual(p.post_path, 'https://backend.example.test/api/v1/passport/auth/login')

    def test_host_override_and_unknown_prefix(self):
        with patch.dict(parserConfig, {'v2board_api_origins':['https://backend.example.test']}):
            p = self.parser(["window.settings={host:'https://backend.example.test'};", BUNDLE])
            self.assertEqual(p.post_path, 'https://backend.example.test/api/v1/passport/auth/login')
        with self.assertRaises(ParseIssue):
            self.parser([BUNDLE.replace('c=!1', 'c=unknown')])
        with self.assertRaises(ParseIssue):
            self.parser(["window.settings={host:dynamic()};", BUNDLE])
        p=Parser('http://example.test/');p.response_url=p.url;p.resp_content=''
        with patch('parse.parser.load_scripts', return_value=['// '+BUNDLE.replace('\n',' ')]):
            self.assertFalse(p.json_login_parser())

    def test_commented_config_and_literal_url(self):
        config = "window.defaultConfig = {\n// API\napiUrl: 'https://backend.example.test',\n/* theme */ theme:{color:'red'},\n};window.settings={};"
        with patch.dict(parserConfig, {'v2board_api_origins':['https://backend.example.test']}):
            p=self.parser([config, INTERCEPTOR, BUNDLE])
        self.assertEqual(p.post_path,'https://backend.example.test/api/v1/passport/auth/login')

    def test_generic_api_domains_use_declared_host_not_hardcoded_domain(self):
        with patch.dict(parserConfig, {'v2board_api_origins':[], 'v2board_api_origin_patterns':['https://api.*.*']}):
            for host in ('api.example.test','api.other.test','api.eu.example.test'):
                config=CONFIG.replace('backend.example.test',host)
                p=self.parser([config,INTERCEPTOR,BUNDLE])
                self.assertEqual(p.post_path,f'https://{host}/api/v1/passport/auth/login')
                p=self.parser([f"window.settings={{host:'https://{host}'}};",BUNDLE])
                self.assertEqual(p.post_path,f'https://{host}/api/v1/passport/auth/login')

    def test_api_pattern_rejects_wrong_scheme_port_and_host(self):
        with patch.dict(parserConfig, {'v2board_api_origins':[], 'v2board_api_origin_patterns':['https://api.*.*']}):
            for api in ('http://api.example.test','https://api.example.test:8443','https://notapi.example.test',
                        'https://api-example.test','https://api.localhost','https://api.example.test@other.test'):
                config=CONFIG.replace('https://backend.example.test',api)
                with self.subTest(api=api),self.assertRaises(ParseIssue):
                    self.parser([config,INTERCEPTOR,BUNDLE])
            with patch.dict(parserConfig, {'v2board_api_origin_patterns':[]}):
                with self.assertRaises(ParseIssue):
                    self.parser([CONFIG.replace('backend.example.test','api.example.test'),INTERCEPTOR,BUNDLE])

    def test_api_pattern_explicit_port_and_invalid_rules(self):
        from parse.v2board import api_origin_pattern_matches
        self.assertTrue(api_origin_pattern_matches('https://api.example.test:8443/path',['https://api.*.*:8443']))
        self.assertFalse(api_origin_pattern_matches('https://api.example.test:8443/path',['https://api.*.*']))
        for pattern in ('*','https://*','https://api.*.*/path','https://api.*.*@evil.test',None):
            self.assertFalse(api_origin_pattern_matches('https://api.example.test/path',[pattern]))

    def test_success_requires_session_not_subscription_token(self):
        task=CrackTask();task.parser=self.parser()
        for body, expected in [({'data':{'auth_data':'fixture-session','token':'subscription'}},LoginState.SUCCESS),
                               ({'data':{'token':'subscription'}},LoginState.UNKNOWN),
                               ({'data':{'auth_data':''}},LoginState.UNKNOWN),
                               ({'data':{'auth_data':True}},LoginState.UNKNOWN),
                               ({'data':{'auth_data':None}},LoginState.UNKNOWN)]:
            res=response(json.dumps(body),content_type='application/json')
            self.assertEqual(task.classify_response(res),expected)
        baseline=response('{"data":{"auth_data":"public"}}',content_type='application/json')
        task.baseline_responses=[baseline]
        self.assertEqual(task.classify_response(response('{"data":{"auth_data":"candidate"}}',content_type='application/json')),LoginState.UNKNOWN)

    def test_structured_failure_rate_limit_and_real_server_error(self):
        task=CrackTask();task.parser=self.parser()
        for status, body, expected in [
            (500,{'message':'Incorrect email or password'},LoginState.FAILURE),
            (422,{'message':'validation','errors':{'email':['invalid']}},LoginState.FAILURE),
            (500,{'message':'There are too many password errors, please try again after 60 minutes.'},LoginState.STOPPED),
            (500,{'message':'邮箱或密码错误'},LoginState.FAILURE),
            (500,{'message':'database unavailable'},LoginState.ERROR),
            (429,{},LoginState.STOPPED)]:
            res=response(json.dumps(body,ensure_ascii=False),status=status,content_type='application/json')
            self.assertEqual(task.classify_response(res),expected)
        conn=Mock();conn.post.return_value=response('{"message":"Incorrect email or password"}',status=500,content_type='application/json')
        with patch('crack.crack_task.time.sleep'):
            task.crack_request(conn,'fixture@example.test','wrongpass')
        self.assertEqual(conn.post.call_count,1)

    def test_refresh_preserves_adapter(self):
        original=self.parser();fresh=Parser(original.url)
        def get_page():
            fresh.response_url=original.response_url;fresh.resp_content=original.resp_content
        with patch.object(fresh,'get_resp_content',get_page):
            self.assertTrue(fresh.refresh_from(original))
        self.assertEqual(fresh.site_adapter,'v2board')
        self.assertEqual(fresh.json_token_fields,['data.auth_data'])

    def test_loopback_large_bundle_and_verified_login(self):
        from urllib.parse import parse_qs, urlsplit
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,body,content,status=200):
                raw=body.encode();self.send_response(status);self.send_header('Content-Type',content)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                if urlsplit(self.path).path=='/assets/umi.js':
                    self.send('/*'+'x'*2400000+'*/'+BUNDLE,'application/javascript')
                else:self.send(SHELL,'text/html')
            def do_POST(self):
                data=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                seen.append((self.path,self.headers['Content-Type'],data))
                if data=={'email':['fixture@example.test'],'password':['fixturepass']}:
                    self.send('{"data":{"auth_data":"fixture-session","token":"subscription"}}','application/json')
                else:self.send('{"message":"Incorrect email or password"}','application/json',500)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        real_session=requests.Session
        def session():
            s=real_session();s.trust_env=False;return s
        try:
            url=f'http://127.0.0.1:{server.server_port}/#/login'
            for concurrency in (1, 2):
                with patch('requests.session',side_effect=session),patch.dict(crackConfig,{'delay':0,'concurrency':concurrency}), \
                     patch('crack.crack_task.gen_dict',return_value=(['fixture@example.test'],['wrongpass','fixturepass'])), \
                     patch.dict(generatorConfig['dict_config']['sqlin_dict'],{'enable':False}),contextlib.redirect_stdout(io.StringIO()):
                    result=CrackTask().run(1,url)
                self.assertEqual(result,{'url':url,'username':'fixture@example.test','password':'fixturepass'})
            self.assertTrue(all(path=='/api/v1/passport/auth/login' and content.startswith('application/x-www-form-urlencoded') for path,content,_ in seen))
            self.assertGreaterEqual(len(seen),5)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
