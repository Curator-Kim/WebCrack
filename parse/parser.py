from urllib.parse import urljoin, urlsplit
import re
from copy import deepcopy
from parse.recognizers import discover_candidates, same_origin, calls, environment, object_fields, string_value, success_rules
from parse.resources import load_scripts
from parse.v2board import recognize_v2board, V2BoardIssue
import captcha_solver
from conf.config import *
import requests
from http_requests import TaskStopped, request_with_timeout_retries
from bs4 import BeautifulSoup as BS
from generator.header import get_random_headers
import logs.log as Log
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


class ParseIssue(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Parser:
    id = 0
    url = ''
    post_path = ''
    resp_content = ''
    form_content = ''
    username_keyword = ''
    password_keyword = ''
    data = ''
    cms = ''
    captcha_required = False
    captcha_field = ''
    captcha_image_url = ''
    captcha_length = None

    def __init__(self, url, session=None):
        self.script_cache = {}
        self.resource_warnings = []
        self.resource_count = 0
        self.diagnostic_code = "NOT_RUN"
        self.candidates = []
        self.json_token_fields = None
        self.profile_success_fields = None
        self.request_headers = {}
        self.timeout = crackConfig["timeout"]
        self.headers = get_random_headers
        self.login_scripts = []
        self.json_response_success = {}
        self.json_required_nonempty_fields = []
        self.request_format = "form"
        self.site_adapter = ""
        self.captcha_required = False
        self.captcha_field = ''
        self.captcha_image_url = ''
        self.captcha_length = None
        self.session = session
        self.url = url
        self.requests_proxies = crackConfig["requests_proxies"]

    def run(self):
        try:
            if self.apply_site_profile():
                self.diagnostic_code = "PROFILE_APPLIED"
                return True
            self.get_resp_content()
            self.cms_parser()
            if self.json_login_parser():
                self.captcha_parser(soft=True)
                self.diagnostic_code = "INTERFACE_FOUND"
                return True
            if self.discover_login_entry():
                self.cms_parser()
                if self.json_login_parser():
                    self.captcha_parser(soft=True)
                    self.diagnostic_code = "INTERFACE_FOUND"
                    return True
            self.form_parser()
            self.check_login_page()
            self.captcha_parser()
            self.post_path_parser()
            self.param_parser()
            self.jquery_login_parser()
            self.diagnostic_code = "FORM_FOUND"
        except TaskStopped:
            raise
        except Exception as e:
            self.diagnostic_code = getattr(e, "code", "PAGE_HTTP_ERROR" if isinstance(e,requests.RequestException) else "PARSE_ERROR")
            Log.Error(f"[-] {self.url} Parse Error [{self.diagnostic_code}]: " + str(e))
            return False
        return True

    @staticmethod
    def _script_signature(content):
        soup = BS(content, "lxml")
        return tuple((tag.get("src", ""), tag.get("type", ""),
                      "" if tag.get("src") else tag.get_text())
                     for tag in soup.find_all("script")) + tuple(
                         (tag.get("href", ""), tuple(tag.get("rel", [])), tag.get("as", ""))
                         for tag in soup.find_all("link", href=True)
                         if any(rel in ("modulepreload", "preload") for rel in tag.get("rel", [])))

    @staticmethod
    def _form_signature(form):
        if not form or not hasattr(form, "find_all"):
            return None
        return (form.get("id", ""), form.get("action", ""), form.get("method", "").lower(),
                tuple((field.get("name", ""), field.get("type", "text").lower(),
                       field.has_attr("disabled")) for field in form.find_all("input")))

    def refresh_from(self, template):
        """刷新页面状态；结构未变时复用任务内接口映射，不重复扫描脚本。"""
        for field in ("post_path", "request_format", "username_keyword", "password_keyword",
                      "data", "request_headers", "cms", "json_token_fields",
                      "profile_success_fields", "json_response_success",
                      "json_required_nonempty_fields", "captcha_required",
                      "captcha_field", "captcha_image_url", "captcha_length", "site_adapter"):
            setattr(self, field, deepcopy(getattr(template, field)))
        self.script_cache = dict(template.script_cache)
        self.login_scripts = list(template.login_scripts)
        if template.diagnostic_code == "PROFILE_APPLIED":
            self.diagnostic_code = "PROFILE_APPLIED"
            return True
        try:
            # 已发现的登录入口直接刷新，不重新访问首页及发现入口。
            original_url = self.url
            self.url = getattr(template, "response_url", template.url)
            try:
                self.get_resp_content()
            finally:
                self.url = original_url
            changed = self.response_url != getattr(template, "response_url", template.url)
            changed |= self._script_signature(self.resp_content) != self._script_signature(template.resp_content)
            changed |= tuple(keyword["keywords"] in self.resp_content for keyword in cmsConfig.values()) != tuple(
                keyword["keywords"] in template.resp_content for keyword in cmsConfig.values())
            if not changed and self.request_format == "form" and not self.site_adapter:
                self.form_parser()
                changed = self._form_signature(self.form_content) != self._form_signature(template.form_content)
                if not changed:
                    self.check_login_page()
                    self.captcha_parser()
                    self.param_parser()  # Cookie、隐藏字段及勾选状态来自新页面。
            if changed:
                self.script_cache.clear()
                Log.Info(f"[*] 登录页面结构变化，重新识别接口: {self.response_url}")
                fresh = Parser(self.url, session=self.session)
                fresh.stop_event = getattr(self, "stop_event", None)
                result = fresh.run()
                self.__dict__.clear()
                self.__dict__.update(fresh.__dict__)
                return result
            self.diagnostic_code = "PLAN_REFRESHED"
            return True
        except TaskStopped:
            raise
        except Exception as exc:
            self.diagnostic_code = getattr(exc, "code", "PAGE_HTTP_ERROR" if isinstance(exc, requests.RequestException) else "PARSE_ERROR")
            Log.Error(f"[-] {self.url} Refresh Error [{self.diagnostic_code}]: {exc}")
            return False

    def apply_site_profile(self):
        matches = [profile for profile in parserConfig.get("site_profiles", [])
                   if profile.get("page_url") == self.url]
        if not matches:
            return False
        if len(matches) != 1:
            raise ParseIssue("AMBIGUOUS_PROFILE", "存在多个匹配的接口配置")
        profile = matches[0]
        endpoint = urljoin(self.url, profile.get("endpoint", ""))
        if not profile.get("endpoint") or not same_origin(self.url, endpoint):
            raise ParseIssue("INVALID_PROFILE", "接口配置需要同源的明确 endpoint")
        if profile.get("encoding", "json") not in ("json", "form"):
            raise ParseIssue("INVALID_PROFILE", "encoding 仅支持 json 或 form")
        username, password = profile.get("username_field"), profile.get("password_field")
        if not isinstance(username,str) or not isinstance(password,str) or not username or not password or username == password:
            raise ParseIssue("INVALID_PROFILE", "需要不同的用户名和密码字段")
        data = profile.get("extra_data", {})
        if not isinstance(data,dict):
            raise ParseIssue("INVALID_PROFILE", "extra_data 需要为对象")
        headers = profile.get("headers", {})
        if not isinstance(headers,dict) or any(not isinstance(key,str) or not isinstance(value,str) for key,value in headers.items()):
            raise ParseIssue("INVALID_PROFILE", "headers 需要字符串键值对象")
        expected_content = "application/json" if profile.get("encoding", "json") == "json" else "application/x-www-form-urlencoded"
        for key,value in headers.items():
            if key.lower() == "content-type" and value.split(";",1)[0].strip().lower() != expected_content:
                raise ParseIssue("INVALID_PROFILE", "Content-Type 与 encoding 不一致")
        success = profile.get("success_fields", {})
        tokens = profile.get("token_fields", [])
        required = profile.get("required_token_fields", [])
        if not isinstance(success,dict) or not isinstance(tokens,list) or not isinstance(required,list):
            raise ParseIssue("INVALID_PROFILE", "成功规则类型错误")
        if any(not isinstance(path,str) or not path for path in list(success)+tokens+required):
            raise ParseIssue("INVALID_PROFILE", "成功字段路径需要非空字符串")
        captcha = profile.get("captcha")
        if captcha is not None:
            if not isinstance(captcha, dict) or not captcha.get("field") or not captcha.get("image_url"):
                raise ParseIssue("INVALID_PROFILE", "captcha 需要 field 与 image_url")
            if not captchaConfig.get("enable", True):
                raise ParseIssue("CAPTCHA_REQUIRED", "已通过 --no-captcha 关闭自动验证码识别")
            if not captcha_solver.available():
                raise ParseIssue("CAPTCHA_REQUIRED", "ddddocr 未安装或不可用，自动验证码识别不可用")
            length = captcha.get("length")
            if length is not None and (isinstance(length, bool) or not isinstance(length, int) or length <= 0):
                raise ParseIssue("INVALID_PROFILE", "captcha.length 需要正整数")
        self.post_path = endpoint
        self.request_format = profile.get("encoding", "json")
        self.username_keyword, self.password_keyword = username,password
        self.data = data.copy()
        self.request_headers = headers.copy()
        self.profile_success_fields = success.copy()
        self.json_token_fields = tokens[:]
        self.json_required_nonempty_fields = required[:]
        if captcha is not None:
            self.captcha_required = True
            self.captcha_field = captcha["field"]
            self.captcha_image_url = captcha["image_url"]
            self.captcha_length = captcha.get("length")
        Log.Info(f"[*] 使用配置式接口: {endpoint}")
        return True

    def discover_login_entry(self):
        if not parserConfig.get("discover_login_links", True):
            return False
        soup = BS(self.resp_content, "lxml")
        if soup.find("input",attrs={"type":lambda value:value and value.lower()=="password"}):
            return False
        links = set()
        for link in soup.find_all("a",href=True):
            target = urljoin(self.response_url,link["href"])
            path = urlsplit(target).path
            if same_origin(self.response_url,target) and target != self.response_url and (
                re.search(r"/(?:login|signin|sign-in|toLogin)(?:/|$)",path,re.I)
                or link.get_text(strip=True) in ("登录","登陆","Login","Sign in")
            ):
                links.add(target)
        if not links:
            return False
        if len(links) != 1:
            raise ParseIssue("AMBIGUOUS_ENTRY", "存在多个同源登录入口，请指定登录页地址")
        endpoint = links.pop()
        client = self.session if self.session is not None else requests
        # One linked page only, no crawling or automatic redirect expansion.
        res = request_with_timeout_retries(
            lambda: client.get(endpoint, timeout=self.timeout, verify=False, headers=self.headers(),
                               proxies=self.requests_proxies, allow_redirects=False),
            context=f"登录入口 GET {endpoint}", stop_event=getattr(self, "stop_event", None))
        if res.status_code != 200:
            raise ParseIssue("ENTRY_HTTP_ERROR", f"登录页 HTTP {res.status_code}")
        res.encoding = res.apparent_encoding
        self.response_url, self.resp_content = endpoint,res.text
        self.cms = ''
        Log.Info(f"[*] 发现同源登录页面: {endpoint}")
        return True

    def get_resp_content(self):
        client = self.session if self.session is not None else requests
        res = request_with_timeout_retries(
            lambda: client.get(self.url, timeout=crackConfig["timeout"], verify=False, headers=get_random_headers(),
                               proxies=self.requests_proxies),
            context=f"页面 GET {self.url}", stop_event=getattr(self, "stop_event", None))
        res.raise_for_status()
        res.encoding = res.apparent_encoding
        self.response_url = res.url
        self.resp_content = res.text

    def cms_parser(self):
        for cms in cmsConfig.values():
            keyword = cms["keywords"]
            if keyword and (keyword in self.resp_content):
                Log.Info(f"[*] {self.url} 识别到cms: {cms['name']}")
                if cms['alert']:
                    Log.Info(f"[*] {self.url} {cms['note']}")
                self.cms = cms

    def _apply_inferred_success_rules(self, sources):
        """接口未自带响应规则时，从静态回调的显式成功条件补齐判定规则。"""
        if self.json_response_success:
            return False
        rules = success_rules(sources)
        if not rules:
            return False
        self.json_response_success = rules
        Log.Info(f"[*] 识别静态回调成功规则: {sorted(rules)}")
        return True

    def json_login_parser(self):
        """识别静态 fetch + JSON.stringify，不执行 JS，不跨域获取脚本。"""
        if not parserConfig.get("json_login_detection", True):
            return False
        scripts = load_scripts(self, parserConfig)
        self.login_scripts = scripts
        try:
            adapter = recognize_v2board(scripts, self.response_url, parserConfig)
        except V2BoardIssue as exc:
            raise ParseIssue(exc.code, str(exc)) from exc
        if adapter:
            self.post_path = adapter
            self.request_format = "form"
            self.username_keyword, self.password_keyword = "email", "password"
            self.data = {}
            self.site_adapter = "v2board"
            self.request_headers = {"Accept": "application/json"}
            # token 是订阅凭据；auth_data 才是登录会话，避免将订阅 token 当成登录成功。
            self.profile_success_fields = {}
            self.json_token_fields = ["data.auth_data"]
            self.json_required_nonempty_fields = ["data.auth_data"]
            Log.Info(f"[*] 识别 V2Board 登录接口: {adapter} (email/password, form)")
            return True
        self.candidates = discover_candidates(scripts,self.response_url,parserConfig)
        if len(self.candidates) > 1:
            raise ParseIssue("AMBIGUOUS_INTERFACE", "多个接口或字段映射候选，请使用 site_profiles 明确指定")
        if self.candidates:
            candidate = self.candidates[0]
            self.post_path,self.request_format = candidate.endpoint,candidate.encoding
            self.username_keyword,self.password_keyword = candidate.username,candidate.password
            self.data = candidate.data.copy()
            # Preserve the stronger, already tested response-wrapper adapter where applicable.
            if candidate.encoding == "json":
                endpoint = candidate.endpoint
                if self.axios_login_parser(scripts) and self.post_path != endpoint:
                    raise ParseIssue("AMBIGUOUS_INTERFACE", "静态规则与包装器规则指向不同接口")
                # 包装器只补充响应规则；字段映射仍以静态候选为准，避免被覆盖。
                self.username_keyword, self.password_keyword = candidate.username, candidate.password
                self.data = candidate.data.copy()
            self._apply_inferred_success_rules(scripts)
            Log.Info(f"[*] 静态接口识别: {self.post_path} ({','.join(candidate.evidence)})")
            return True
        return self.axios_login_parser(scripts)

    def axios_login_parser(self, scripts):
        """识别 Axios 字面量 baseURL、登录调用链和 Token 响应包装。"""
        base = self.response_url
        origin = urlsplit(base)
        candidates = {}
        creation = r"(?<![\w$.])(\w+)\s*=\s*[\w$.]+\.create\(\s*\{\s*baseURL\s*:\s*(['\"])([^'\"]+)\2"
        for script in scripts:
            for match in re.finditer(creation, script):
                client, prefix = match.group(1, 3)
                api_pattern = (r"(?<![\w$.])(\w+)\s*=\s*\{\s*login\s*:\s*async\s+(\w+)\s*=>\s*"
                               + re.escape(client) + r"\.post\(\s*(['\"])([^'\"]+)\3\s*,\s*\2\s*\)")
                for api in re.finditer(api_pattern, script):
                    api_name, path = api.group(1, 4)
                    if not re.search(r"login|sign[-_]?in", path, re.I):
                        continue
                    usage = (r"(?:const|let|var)\s+(\w+)\s*=\s*await\s+" + re.escape(api_name)
                             + r"\.login\(\s*(\w+)\s*\)\s*;\s*localStorage\.setItem\(\s*(['\"])token\3\s*,\s*\1\s*\)")
                    call = re.search(usage, script)
                    if not call:
                        continue
                    model = re.escape(call.group(2))
                    if not re.search(r"(?<![\w$.])" + model + r"\s*=\s*\w+\(\s*\{\s*username\s*:\s*(['\"])\1\s*,\s*password\s*:\s*(['\"])\2\s*\}", script):
                        continue
                    endpoint = urljoin(base, prefix.rstrip('/') + '/' + path.lstrip('/'))
                    parsed = urlsplit(endpoint)
                    if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                        continue
                    # Axios 响应拦截器明确从 code=200 的 data 中返回 Token。
                    wrapper = (re.escape(client) + r"\.interceptors\.response\.use\(\s*(\w+)\s*=>\s*\{\s*const\s*"
                               r"\{\s*code\s*:\s*(\w+)\s*,\s*message\s*:\s*\w+\s*,\s*data\s*:\s*(\w+)\s*\}\s*=\s*\1\.data\s*;\s*return\s+\2\s*===\s*200\s*\?\s*\3")
                    candidates.setdefault(endpoint, []).append(bool(re.search(wrapper, script)))
        if len(candidates) != 1:
            return False
        self.post_path, wrappers = next(iter(candidates.items()))
        self.username_keyword, self.password_keyword = "username", "password"
        self.data = {}
        self.request_format = "json"
        if all(wrappers):
            self.json_response_success = {"code": [200]}
            self.json_required_nonempty_fields = ["data"]
        else:
            self._apply_inferred_success_rules(scripts)
        Log.Info(f"[*] 识别 Axios JSON 登录接口: {self.post_path}")
        return True

    def jquery_ajaxsubmit_parser(self):
        """识别 jQuery Form Plugin `.ajaxSubmit(callback)` 回调中的成功条件。

        仅采纳回调内明确表达的条件：`ret.success` 为真或 `ret.code == 200`，
        不执行 JS，也不从响应本体推断成功。
        """
        form_id = self.form_content.get("id")
        call = re.compile(r"\.\s*ajaxSubmit\s*\(\s*function\s*\(\s*([\w$]+)\s*(?:,[^)]*)?\)\s*\{", re.S)
        selector = re.compile(r"\$\(\s*(['\"])([^'\"]{0,120}?)\1\s*\)\s*$")
        for script in self.login_scripts:
            for match in call.finditer(script):
                head = script[max(0, match.start() - 160):match.start()]
                selectors = [item.group(2).strip() for item in selector.finditer(head)]
                if form_id and selectors and not any(
                        value.lstrip("#") == form_id or value == "form" for value in selectors):
                    continue
                parameter = re.escape(match.group(1))
                body = script[match.end():match.end() + 1500]
                truthy = re.search(r"(?<![!\w$.])" + parameter + r"\s*\.\s*success\b\s*\)", body)
                code_ok = re.search(r"(?<![!\w$.])" + parameter
                                    + r"\s*\.\s*code\b\s*={2,3}\s*['\"]?200['\"]?\s*\)", body)
                rules = {}
                if truthy:
                    rules["success"] = [True]
                if code_ok:
                    rules["code"] = [200, "200"]
                if rules:
                    self.json_response_success = rules
                    Log.Info(f"[*] 识别 ajaxSubmit 成功规则: {sorted(rules)}")
                    return True
        return False

    def jquery_login_parser(self):
        """识别当前表单 serialize() 的 $.post 和字面量 URL，不执行 JS。"""
        form_id = self.form_content.get("id")
        if not form_id:
            return self.jquery_ajaxsubmit_parser()
        constants = {}
        for script in self.login_scripts:
            for match in re.finditer(r"(?:var|let|const)\s+(\w+)\s*=\s*(['\"])([^'\"]*)\2\s*;", script):
                constants.setdefault(match.group(1), set()).add(match.group(3))
        pattern = (r"\$\.post\(\s*(?:(\w+)\s*\+\s*)?(['\"])([^'\"]+)\2\s*,\s*"
                   r"\$\(\s*(['\"])#([^'\"]+)\4\s*\)\.serialize\(\s*\)\s*,")
        base = getattr(self, "response_url", self.url)
        origin = urlsplit(base)
        candidates = {}
        for script in self.login_scripts:
            for match in re.finditer(pattern, script):
                variable, path, target_form = match.group(1, 3, 5)
                if target_form != form_id:
                    continue
                if variable:
                    values = constants.get(variable, set())
                    if len(values) != 1:
                        continue
                    path = next(iter(values)) + path
                endpoint = urljoin(base, path)
                parsed = urlsplit(endpoint)
                if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                    continue
                # 只采纳紧接当前 $.post 的回调明确表达的成功条件。
                callback = script[match.end():match.end() + 1200]
                success = re.match(r"\s*function\s*\(\s*(\w+)[^)]*\)\s*\{\s*if\s*\(\s*\1\.code\s*={2,3}\s*(['\"])200\2\s*\)", callback)
                candidates.setdefault(endpoint, []).append(bool(success))
        static_constants, static_objects = environment(self.login_scripts)
        for script in self.login_scripts:
            for _, args in calls(script,r"(?:\$|jQuery)\.ajax"):
                options = object_fields(args[0]) if args else None
                if not options:
                    options = static_objects.get(args[0].strip()) if args else None
                if not options:
                    continue
                method = string_value(options.get("method",options.get("type","")),static_constants)
                serialized = re.fullmatch(r"(?:\$|jQuery)\(\s*(['\"])#([^'\"]+)\1\s*\)\.serialize\(\s*\)",options.get("data", ""))
                if not method or method.upper() != "POST" or not serialized or serialized[2] != form_id:
                    continue
                path = string_value(options.get("url",""),static_constants)
                if path is None:
                    continue
                endpoint = urljoin(base,path)
                if not same_origin(base,endpoint):
                    continue
                callback = options.get("success", "")
                success = re.match(r"function\s*\(\s*(\w+)[^)]*\)\s*\{\s*if\s*\(\s*\1\.code\s*={2,3}\s*(['\"])200\2\s*\)",callback)
                candidates.setdefault(endpoint,[]).append(bool(success))
        if len(candidates) > 1:
            raise ParseIssue("AMBIGUOUS_INTERFACE", "多个 jQuery 表单提交接口")
        if len(candidates) != 1:
            return self.jquery_ajaxsubmit_parser()
        self.post_path, rules = next(iter(candidates.items()))
        if all(rules):
            self.json_response_success = {"code": [200, "200"]}
        else:
            # 接口已知但回调未给出 code 规则时，补充 ajaxSubmit 与静态回调成功条件。
            self.jquery_ajaxsubmit_parser()
            self._apply_inferred_success_rules(self.login_scripts)
        Log.Info(f"[*] 识别 jQuery 表单登录接口: {self.post_path}")
        return True

    def form_parser(self):
        soup = BS(self.resp_content, "lxml")
        forms = soup.find_all("form")
        if not forms:
            raise ParseIssue("NO_LOGIN_INTERFACE", "No form found；未找到静态登录接口，可能需要动态执行或 site_profiles 配置")
        # 优先含密码输入框的表单，其次同时含用户名、密码命名的表单。
        for form in forms:
            if form.find("input", attrs={"type": lambda value: value and value.lower() == "password"}):
                self.form_content = form
                return
        for form in forms:
            names = [field.get("name", "").lower() for field in form.find_all("input")]
            if (any(any(key in name for key in parserConfig["username_keyword_list"]) for name in names)
                    and any(any(key in name for key in parserConfig["password_keyword_list"]) for name in names)):
                self.form_content = form
                return
        self.form_content = forms[0]

    def check_login_page(self):
        login_keyword_list = parserConfig["login_keyword_list"]
        for login_keyword in login_keyword_list:
            if login_keyword in str(self.form_content).lower():
                return True
        raise Exception("Maybe not login pages")

    @staticmethod
    def _captcha_haystack(content):
        """收集表单/页面的可见文本与标识属性；忽略脚本样式内容。"""
        clone = BS(str(content), "lxml")
        for tag in clone(["script", "style", "noscript"]):
            tag.decompose()
        visible = clone.get_text(" ", strip=True)
        attrs = []
        for tag in clone.find_all(True):
            for name in ("placeholder", "alt", "aria-label", "title", "name", "id", "class"):
                value = tag.get(name, "")
                if isinstance(value, (list, tuple)):
                    value = " ".join(str(item) for item in value)
                attrs.append(str(value))
        return (visible + " " + " ".join(attrs)).casefold()

    @staticmethod
    def _captcha_name_level(name):
        """返回 captcha 输入框名称的匹配强度：strong/weak/空。"""
        if not name:
            return ""
        lowered = str(name).casefold()
        if any(keyword and keyword.casefold() in lowered
               for keyword in captchaConfig.get("field_keyword_list", [])):
            return "strong"
        if any(keyword and keyword.casefold() in lowered
               for keyword in captchaConfig.get("weak_field_keyword_list", [])):
            return "weak"
        return ""

    def _find_captcha_field(self, content):
        """定位验证码输入字段名；优先强关键字，其次弱关键字。"""
        names = []
        for element in content.find_all(["input", "textarea", "select"]):
            for attribute in ("name", "id"):
                value = (element.get(attribute) or "").strip()
                if value and value not in names:
                    names.append(value)
        reserved = parserConfig.get("username_keyword_list", []) + parserConfig.get("password_keyword_list", [])
        for level in ("strong", "weak"):
            for name in names:
                if any(keyword and keyword.casefold() in name.casefold() for keyword in reserved):
                    continue
                if self._captcha_name_level(name) == level:
                    return name
        return ""

    @staticmethod
    def _captcha_length(content, field):
        """读取验证码输入框声明的最长长度；页面未声明时为 None。"""
        if not field:
            return None
        for element in content.find_all(["input", "textarea"]):
            if field in (element.get("name"), element.get("id")):
                for attribute in ("maxlength", "data-length"):
                    value = str(element.get(attribute) or "").strip()
                    if value.isdigit() and 0 < int(value) <= 32:
                        return int(value)
                return None
        return None

    def _find_captcha_image(self, content):
        """定位验证码图片地址；关键字匹配优先，表单内唯一图片作为后备。"""
        base = getattr(self, "response_url", self.url)
        keywords = [keyword for keyword in captchaConfig.get("image_keyword_list", []) if keyword]
        images = [element for element in content.find_all(["img", "input"])
                  if element.name == "img" or element.get("type", "").lower() == "image"]

        def resolve(src):
            # urljoin 对绝对地址、协议相对地址、站内相对路径与 data: URI 均适用。
            return urljoin(base, src.strip())

        for element in images:
            src = (element.get("src") or "").strip()
            if not src:
                continue
            marker = " ".join(str(element.get(attr, "")) for attr in
                              ("src", "id", "class", "alt", "name", "title")).casefold()
            if any(keyword.casefold() in marker for keyword in keywords):
                return resolve(src)
        if content is self.form_content and len(images) == 1:
            src = (images[0].get("src") or "").strip()
            if src:
                return resolve(src)
        return ""

    def captcha_parser(self, soft=False):
        """检测验证码；环境支持 ddddocr 时记录识别所需的字段与图片地址。

        检测到验证码但缺少 ddddocr 或定位不到字段/图片时抛出 CAPTCHA_REQUIRED，
        由调用方放弃该网站。`soft=True` 用于静态接口识别：页面仅提及验证码而
        定位不到可识别图片时按无验证码处理，避免误跳过可静态提交的接口。
        """
        content = self.form_content if self.form_content else BS(self.resp_content, "lxml")
        haystack = self._captcha_haystack(content)
        keyword = next((item for item in parserConfig["captcha_keyword_list"]
                        if item and item.casefold() in haystack), None)
        self.captcha_required = False
        if keyword is None:
            return False
        field = self._find_captcha_field(content)
        image_url = self._find_captcha_image(content)
        if soft and not (field and image_url):
            Log.Info(f"[*] {self.url} 页面包含验证码关键字，但未定位到可识别图片，按无验证码处理")
            return False
        if not captchaConfig.get("enable", True):
            raise ParseIssue("CAPTCHA_REQUIRED", "已通过 --no-captcha 关闭自动验证码识别")
        if not captcha_solver.available():
            raise ParseIssue("CAPTCHA_REQUIRED", "ddddocr 未安装或不可用，自动验证码识别不可用")
        if not field:
            raise ParseIssue("CAPTCHA_REQUIRED", "未能定位验证码输入字段")
        if not image_url:
            raise ParseIssue("CAPTCHA_REQUIRED", "未能定位验证码图片地址")
        self.captcha_required = True
        self.captcha_field = field
        self.captcha_image_url = image_url
        self.captcha_length = self._captcha_length(content, field)
        suffix = f"，长度 {self.captcha_length}" if self.captcha_length else ""
        Log.Info(f"[*] {self.url} 检测到验证码，使用 ddddocr 自动识别字段 {field}{suffix}")
        return True

    def post_path_parser(self):
        base_url = getattr(self, "response_url", self.url)
        action = self.form_content.get("action", "").strip()
        self.post_path = urljoin(base_url, action) if action else base_url

    def param_parser(self):
        content = self.form_content
        data = {}
        username_keyword = ''
        password_keyword = ''
        username_keyword_list = parserConfig["username_keyword_list"]
        password_keyword_list = parserConfig["password_keyword_list"]
        for input_element in content.find_all('input'):
            kind = input_element.get('type', 'text').lower()
            if input_element.has_attr('disabled') or kind in ('reset', 'button', 'submit', 'file'):
                continue
            if kind in ('checkbox', 'radio') and not input_element.has_attr('checked'):
                continue
            if input_element.has_attr('name'):
                parameter = input_element['name']
            else:
                parameter = ''
            if input_element.has_attr('value'):
                value = input_element['value']
            else:
                value = "on" if kind in ("checkbox", "radio") else parserConfig["default_value"]
            if parameter:
                data[parameter] = value

        # 提取username_keyword,password_keyword
        for parameter in data:
            if not username_keyword and parameter != password_keyword:
                for keyword in username_keyword_list:
                    if keyword in parameter.lower():
                        username_keyword = parameter
                        break
            if not password_keyword and parameter != username_keyword:
                for keyword in password_keyword_list:
                    if keyword in parameter.lower():
                        password_keyword = parameter
                        break

        # 弹出reset
        for i in ['reset']:
            for r in list(data.keys()):
                if i in r.lower():
                    data.pop(r)

        if username_keyword and password_keyword:
            self.username_keyword = username_keyword
            self.password_keyword = password_keyword
            self.data = data
        else:
            raise ParseIssue("UNRESOLVED_FIELDS", "登录字段未确定：请检查前端绑定或配置字段映射")
