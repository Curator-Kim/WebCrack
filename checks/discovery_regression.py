import contextlib
import io
import json
import threading
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests
from conf.config import parserConfig, crackConfig
from parse.parser import Parser, ParseIssue
from parse.recognizers import discover_candidates
from parse.resources import load_scripts
from crack.crack_task import CrackTask, LoginState
from checks.login_regression import response


class DiscoveryTests(unittest.TestCase):
    def candidates(self, script):
        return discover_candidates([script], 'https://example.test/page', parserConfig)

    def test_fetch_field_aliases_and_constants(self):
        for user, password in [('username','password'),('userName','passwd'),('account','pwd'),('mobile','pass'),('email','password')]:
            script = '''const cfg={apiBase:'/api'}; const path='/login';
fetch(`${cfg.apiBase}${path}`, {headers:{'Content-Type':'application/json'},
 body:JSON.stringify({USER: input.value, PASS: password.value}), method:'POST'});'''.replace('USER',user).replace('PASS',password)
            candidates = self.candidates(script)
            self.assertEqual(len(candidates),1)
            self.assertEqual((candidates[0].endpoint,candidates[0].username,candidates[0].password),
                             ('https://example.test/api/login',user,password))

    def test_axios_independent_of_token_storage(self):
        scripts = ["axios.post('/api/login',{username,password})",
                   "const body={account,pwd};axios.request({data:body,url:'/api/login',method:'POST'})",
                   "axios({method:'post',url:'/api/login',data:{mobile,password}})",
                   "const client=axios.create({timeout:1000,baseURL:'/api'});client.post('/login',{email,password})"]
        for script in scripts:
            with self.subTest(script=script):
                candidates = self.candidates(script)
                self.assertEqual(len(candidates),1)
                self.assertEqual(candidates[0].endpoint,'https://example.test/api/login')

    def test_jquery_ajax_both_encodings(self):
        for script, encoding in [("$.ajax({url:'/api/login',type:'POST',data:{account,pwd}})",'form'),
                                 ("jQuery.ajax({url:'/api/login',method:'POST',contentType:'application/json',data:JSON.stringify({username,password})})",'json')]:
            candidates = self.candidates(script)
            self.assertEqual(len(candidates),1)
            self.assertEqual(candidates[0].encoding,encoding)

    def test_extra_scalar_preserved_and_dynamic_rejected(self):
        values = self.candidates("axios.post('/api/login',{username,password,remember:true,mode:'web'})")
        self.assertEqual(values[0].data,{'remember':True,'mode':'web'})
        for script in ("axios.post('/api/login',{username,password,csrf:getToken()})",
                       "axios.post('/api/login',{username,password:encrypt(password)})",
                       "axios.post('/api/login',{username,account,password})",
                       "axios.post(getEndpoint(),{username,password})",
                       "axios.post('/login',{username,password},{headers:{'X-CSRF-Token':getToken()}})",
                       "axios({method:'POST',url:'/login',data:{username,password},headers:{'content-type':'application/x-www-form-urlencoded'}})",
                       "axios.post('/api/register',{username,password})",
                       "axios.post('https://other.test/login',{username,password})"):
            self.assertEqual(self.candidates(script),[],script)

    def test_comments_and_strings_not_executed(self):
        self.assertEqual(self.candidates("// axios.post('/login',{username,password})\nconst note=\"axios.post('/login',{username,password})\";"),[])

    def test_conflicting_constants_and_duplicate_evidence(self):
        self.assertEqual(self.candidates("var path='/login';var path='/signin';axios.post(path,{username,password})"),[])
        candidates = self.candidates("axios.post('/login',{username,password});axios.post('/login',{username,password});")
        self.assertEqual(len(candidates),1)
        self.assertEqual(len(candidates[0].evidence),2)

    def test_diagnostic_for_ambiguity(self):
        p = Parser('https://example.test/page')
        p.response_url=p.url
        p.resp_content="<script>axios.post('/login',{username,password});axios.post('/signin',{account,pwd})</script>"
        p.get_resp_content=lambda:None
        with contextlib.redirect_stdout(io.StringIO()): self.assertFalse(p.run())
        self.assertEqual(p.diagnostic_code,'AMBIGUOUS_INTERFACE')

    def test_profile_nested_success_and_missing_null(self):
        url='https://example.test/page'
        profile={'page_url':url,'endpoint':'/sessions','encoding':'json','username_field':'account','password_field':'secret',
                 'extra_data':{'tenant':'test'},'success_fields':{'result.ok':True},'required_token_fields':['result.accessToken']}
        p=Parser(url)
        with patch.dict(parserConfig,{'site_profiles':[profile]}), patch.object(p,'get_resp_content') as get:
            self.assertTrue(p.run());get.assert_not_called()
        self.assertEqual(p.data,{'tenant':'test'})
        task=CrackTask();task.parser=p
        for body, expected in [({'result':{'ok':True,'accessToken':'fixture'}},LoginState.SUCCESS),
                               ({'result':{'ok':True,'accessToken':''}},LoginState.UNKNOWN),
                               ({'result':{'ok':False,'accessToken':'fixture'}},LoginState.UNKNOWN)]:
            self.assertEqual(task.classify_response(response(json.dumps(body),content_type='application/json')),expected)
        p.profile_success_fields={'result.ok':None};p.json_required_nonempty_fields=[]
        self.assertEqual(task.classify_response(response('{}',content_type='application/json')),LoginState.UNKNOWN)

    def test_specific_wrapper_not_bypassed_by_generic_token(self):
        p=Parser('https://example.test/page');p.request_format='json'
        p.username_keyword='username';p.password_keyword='password'
        p.json_response_success={'code':[200]};p.json_required_nonempty_fields=['data']
        task=CrackTask();task.parser=p
        self.assertEqual(task.classify_response(response('{"code":500,"data":"fixture","token":"fixture"}',content_type='application/json')),LoginState.UNKNOWN)

    def test_profile_headers_sent_and_mismatched_encoding_rejected(self):
        url='https://example.test/page'
        profile={'page_url':url,'endpoint':'/sessions','encoding':'json','username_field':'account','password_field':'secret','headers':{'X-Tenant':'fixture'}}
        p=Parser(url)
        with patch.dict(parserConfig,{'site_profiles':[profile]}):self.assertTrue(p.run())
        task=CrackTask();task.parser=p
        conn=Mock();conn.post.return_value=response('{}',content_type='application/json')
        with patch('crack.crack_task.time.sleep'):task.crack_request(conn,'fixture','fixture')
        self.assertEqual(conn.post.call_args.kwargs['headers']['X-Tenant'],'fixture')
        profile['headers']={'Content-Type':'application/x-www-form-urlencoded'}
        with patch.dict(parserConfig,{'site_profiles':[profile]}),contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(Parser(url).run())

    def test_invalid_profiles(self):
        url='https://example.test/page'
        for profiles in ([{'page_url':url,'endpoint':'https://other.test/login'}],
                         [{'page_url':url},{'page_url':url}],
                         [{'page_url':url,'endpoint':'/login','username_field':'x','password_field':'x'}]):
            p=Parser(url)
            with patch.dict(parserConfig,{'site_profiles':profiles}), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(p.run())
            self.assertIn(p.diagnostic_code,('INVALID_PROFILE','AMBIGUOUS_PROFILE'))

    def test_nested_global_token_and_success(self):
        p=Parser('https://example.test/page');p.request_format='json';p.username_keyword='account';p.password_keyword='secret'
        task=CrackTask();task.parser=p
        with patch.dict(crackConfig,{'json_token_fields':['data.accessToken']}):
            self.assertEqual(task.classify_response(response('{"data":{"accessToken":"fixture"}}',content_type='application/json')),LoginState.SUCCESS)

    def test_resources_cross_origin_budget_and_depth(self):
        p=Parser('https://example.test/page');p.response_url=p.url
        p.resp_content='<script type="module" src="/entry.js"></script><script src="https://other.test/login.js"></script>'
        requested=[]
        sources={'https://example.test/entry.js':"import './dep.js';",'https://example.test/dep.js':"import './deep.js';"}
        class Stream:
            status_code=200
            def __init__(self,url):self.url=url
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,size):yield sources[self.url].encode()
        client=Mock()
        def get(url,**kwargs):
            requested.append(url);self.assertFalse(kwargs['allow_redirects']);return Stream(url)
        client.get.side_effect=get;p.session=client
        settings=dict(parserConfig,script_dependency_depth=1,json_script_limit=8)
        scripts=load_scripts(p,settings)
        self.assertEqual(len(scripts),2)
        self.assertEqual(requested,['https://example.test/entry.js','https://example.test/dep.js'])
        p.script_cache.clear();requested.clear()
        scripts=load_scripts(p,dict(settings,script_total_max_bytes=8))
        self.assertEqual(scripts,[])
        self.assertEqual(requested,['https://example.test/entry.js'])

    def test_profile_success_not_overridden_by_global_marker(self):
        p=Parser('https://example.test/page');p.request_format='json'
        p.profile_success_fields={'result.success':True};p.json_token_fields=[]
        p.username_keyword='account';p.password_keyword='secret'
        task=CrackTask();task.parser=p
        with patch.dict(crackConfig,{'success_words':['public-banner'],'json_success_fields':{'code':200}}):
            self.assertEqual(task.classify_response(response('{"code":200,"msg":"public-banner"}',content_type='application/json')),LoginState.UNKNOWN)

    def test_ajax_serialized_form(self):
        p=Parser('https://example.test/page');p.response_url=p.url
        p.resp_content='<form id="login"><input name="account"><input name="pwd" type="password"></form>'
        p.login_scripts=["const cfg={base:'/api'};$.ajax({method:'POST',url:cfg.base+'/login',data:$('#login').serialize(),success:function(data){if(data.code=='200'){}}})"]
        p.form_parser();p.param_parser();p.post_path_parser()
        self.assertTrue(p.jquery_login_parser())
        self.assertEqual(p.post_path,'https://example.test/api/login')

    def test_actual_captcha_only(self):
        p=Parser('https://example.test/page')
        p.resp_content='<form><input name="username"><input type="password" name="password"></form><script>const message="captcha";</script>'
        p.form_parser();p.captcha_parser()
        p.resp_content='<form><input placeholder="验证码"></form>';p.form_parser()
        with self.assertRaises(ParseIssue):p.captcha_parser()

    def test_links_dependencies_cache_and_end_to_end(self):
        events=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                events.append(self.path)
                pages={'/':'<a href="/login">登录</a>',
                       '/login':'<div id="app"></div><script type="module" src="/app.js"></script><script src="https://external.test/auth.js"></script><link rel="modulepreload" href="/app.js">',
                       '/app.js':"import './auth-chunk.js';export const unrelated=true;",
                       '/auth-chunk.js':"const options={method:'POST',url:'/api/login',data:{account,pwd}};axios.request(options);"}
                raw=pages.get(self.path,'').encode()
                self.send_response(200 if self.path in pages else 404);self.send_header('Content-Type','application/javascript' if self.path.endswith('.js') else 'text/html');self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                events.append(self.path)
                success=body=={'account':'fixture','pwd':'fixturepass'}
                raw=json.dumps({'data':{'accessToken':'fixture-token'}} if success else {'error':'密码错误'},ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        factory=requests.Session
        def session():
            s=factory();s.trust_env=False;return s
        try:
            url=f'http://127.0.0.1:{server.server_port}/'
            with session() as s, contextlib.redirect_stdout(io.StringIO()):
                p=Parser(url,session=s);self.assertTrue(p.run())
                self.assertEqual(p.post_path,url+'api/login')
                before=events[:];load_scripts(p,parserConfig)
                self.assertEqual(events,before)
                self.assertEqual(p.resource_count,2)
            with patch('requests.session',side_effect=session),patch.dict(crackConfig,{'delay':0,'requests_proxies':{},'json_token_fields':['data.accessToken']}), \
                 patch('crack.crack_task.gen_dict',return_value=(['fixture'],['wrong-fixture','fixturepass'])), \
                 patch('crack.crack_task.Log'),contextlib.redirect_stdout(io.StringIO()):
                result=CrackTask().run(1,url)
            self.assertEqual(result,{'url':url,'username':'fixture','password':'fixturepass'})
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
