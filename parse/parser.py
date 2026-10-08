from urllib.parse import urljoin, urlsplit
import re
from conf.config import *
import requests
from bs4 import BeautifulSoup as BS
from generator.header import get_random_headers
import logs.log as Log
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


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

    def __init__(self, url, session=None):
        self.login_scripts = []
        self.json_response_success = {}
        self.json_required_nonempty_fields = []
        self.request_format = "form"
        self.session = session
        self.url = url
        self.requests_proxies = crackConfig["requests_proxies"]

    def run(self):
        try:
            self.get_resp_content()
            self.cms_parser()
            if self.json_login_parser():
                return True
            self.form_parser()
            self.check_login_page()
            self.captcha_parser()
            self.post_path_parser()
            self.param_parser()
            self.jquery_login_parser()
        except Exception as e:
            Log.Error(f"[-] {self.url} Parse Error: " + str(e))
            return False
        return True

    def get_resp_content(self):
        client = self.session if self.session is not None else requests
        res = client.get(self.url, timeout=crackConfig["timeout"], verify=False, headers=get_random_headers(),
                           proxies=self.requests_proxies)
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

    def json_login_parser(self):
        """识别静态 fetch + JSON.stringify，不执行 JS，不跨域获取脚本。"""
        if not parserConfig.get("json_login_detection", True):
            return False
        soup = BS(self.resp_content, "lxml")
        base = self.response_url
        origin = urlsplit(base)
        scripts = [tag.get_text() for tag in soup.find_all("script") if not tag.get("src")]
        client = self.session if self.session is not None else requests
        sources = []
        for tag in soup.find_all("script", src=True):
            source = urljoin(base, tag["src"])
            parsed = urlsplit(source)
            if ((parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc)
                    and (tag.get("type", "").lower() == "module" or
                         any(word in parsed.path.lower() for word in ("auth", "login")))):
                sources.append(source)
        for source in list(dict.fromkeys(sources))[:parserConfig.get("json_script_limit", 4)]:
            try:
                with client.get(source, timeout=crackConfig["timeout"], verify=False,
                                proxies=self.requests_proxies, allow_redirects=False, stream=True) as res:
                    if res.status_code != 200:
                        continue
                    maximum = parserConfig.get("json_script_max_bytes", 2097152)
                    chunks = []
                    total = 0
                    for chunk in res.iter_content(8192):
                        total += len(chunk)
                        if total > maximum:
                            break
                        chunks.append(chunk)
                    else:
                        scripts.append(b"".join(chunks).decode("utf-8", errors="replace"))
            except requests.RequestException:
                continue
        self.login_scripts = scripts
        candidates = set()
        pattern = r"fetch\(\s*(['\"])([^'\"]+)\1\s*,\s*\{([\s\S]{0,3000}?)JSON\.stringify\(\s*\{([^}]+)\}"
        for script in scripts:
            for match in re.finditer(pattern, script):
                path, options, fields = match.group(2, 3, 4)
                if not re.search(r"method\s*:\s*['\"]POST['\"]", options, re.I):
                    continue
                if "application/json" not in options or not re.search(r"login|sign[-_]?in", path, re.I):
                    continue
                keys = [piece.strip().split(":", 1)[0].strip("\"'") for piece in fields.split(",")]
                if not {"username", "password"}.issubset(keys):
                    continue
                endpoint = urljoin(base, path)
                parsed = urlsplit(endpoint)
                if (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc):
                    candidates.add(endpoint)
        if not candidates:
            return self.axios_login_parser(scripts)
        if len(candidates) != 1:
            return False
        self.post_path = candidates.pop()
        self.username_keyword, self.password_keyword = "username", "password"
        self.data = {}
        self.request_format = "json"
        Log.Info(f"[*] 识别 JSON 登录接口: {self.post_path}")
        return True

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
        Log.Info(f"[*] 识别 Axios JSON 登录接口: {self.post_path}")
        return True

    def jquery_login_parser(self):
        """识别当前表单 serialize() 的 $.post 和字面量 URL，不执行 JS。"""
        form_id = self.form_content.get("id")
        if not form_id:
            return False
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
        if len(candidates) != 1:
            return False
        self.post_path, rules = next(iter(candidates.items()))
        if all(rules):
            self.json_response_success = {"code": [200, "200"]}
        Log.Info(f"[*] 识别 jQuery 表单登录接口: {self.post_path}")
        return True

    def form_parser(self):
        soup = BS(self.resp_content, "lxml")
        forms = soup.find_all("form")
        if not forms:
            raise ValueError("No form found")
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

    def captcha_parser(self):
        captcha_keyword_list = parserConfig["captcha_keyword_list"]
        for captcha in captcha_keyword_list:
            if captcha in self.resp_content.lower():
                raise Exception(f"{captcha} in login page")

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
            raise Exception("Can not get login parameter")
