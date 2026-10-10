import re
from urllib.parse import urljoin, urlsplit

from parse.recognizers import mask, object_fields, string_value, same_origin
from parse.resources import _assignment_expression


class V2BoardIssue(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _without_comments(source):
    tokens = r'"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/'
    return re.sub(tokens, lambda m: ' ' if m[0].startswith(('//', '/*')) else m[0], source)


def _objects(sources, name):
    result = []
    for source in sources:
        for match in re.finditer(r'(?<![\w$.])' + re.escape(name) + r'\s*=\s*', mask(source)):
            expression = _assignment_expression(source, match.end())
            fields = object_fields(_without_comments(expression or ''))
            if fields is None:
                raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', f'{name} 不是静态对象')
            result.append(fields)
    return result


def _literal_field(objects, key):
    values = []
    for fields in objects:
        if key not in fields:
            continue
        value = string_value(fields[key], {})
        if value is None:
            raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', f'{key} 不是字符串字面量')
        values.append(value)
    if len(set(values)) > 1:
        raise V2BoardIssue('AMBIGUOUS_INTERFACE', f'存在多个 {key} 配置')
    return values[0] if values else None


def api_origin_pattern_matches(endpoint, patterns):
    """仅支持 api.*.* 主机模式；scheme/port 必须匹配，不对完整 URL 做 glob。"""
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ''
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if parsed.username or parsed.password or not re.fullmatch(r'api\.' + label + r'(?:\.' + label + r')+', host):
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return False
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        rule = re.fullmatch(r'(https?)://api\.\*\.\*(?::([0-9]{1,5}))?', pattern)
        if not rule:
            continue
        expected_port = int(rule[2]) if rule[2] else (443 if rule[1] == 'https' else 80)
        if rule[1] == parsed.scheme and port == expected_port:
            return True
    return False


def recognize_v2board(sources, page_url, config):
    # 三项证据同时存在：明确登录调用/字段、会话消费、表单 POST 包装。
    bundles = []
    login = r'(?:Object\([\w$]+\[(?:"b"|\x27b\x27)\]\)|[\w$.]+)\s*\(\s*([\x27"])/passport/auth/login\1\s*,\s*\{\s*email\s*:\s*[\w$.]+\s*,\s*password\s*:\s*[\w$.]+\s*\}'
    for source in sources:
        masked = mask(source)
        # 从固定路径向两侧读取有界调用，避免在巨大压缩包/注释中回溯扫描标识符。
        found = False
        for path in re.finditer(r'([\x27"])/passport/auth/login\1', source):
            start = max(0, path.start() - 100)
            for call in re.finditer(login, source[start:path.end() + 256]):
                offset = start + call.start()
                if masked[offset:offset + 6].strip():
                    found = True
        if not found:
            continue
        if not re.search(r'\.data\.auth_data\b', masked):
            continue
        if not re.search(r'method\s*:\s*([\x27"])POST\1', source):
            continue
        if not re.search(r'([\x27"])application/x-www-form-urlencoded\1', source):
            continue
        bundles.append(source)
    if not bundles:
        return None

    settings = _objects(sources, 'window.settings')
    defaults = _objects(sources, 'window.defaultConfig')
    host = _literal_field(settings, 'host')
    if host is None and any('host' in fields for fields in settings):
        raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', '动态 host 需要 site_profiles')
    prefixes = set()
    for source in bundles:
        # 明确静态 serviceHost 也适用于简化构建。
        for match in re.finditer(r'\bserviceHost\s*:\s*([\x27"])([^\x27"]+)\1', source):
            if mask(source)[match.start():match.start()+11].strip():
                prefixes.add(urljoin(page_url, match[2]))
        # 已观察到的生产构建：flag=false, origin=URL(...).origin；host 可覆盖。
        prod = re.search(r'\b([\w$]+)\s*=\s*!1\s*,\s*([\w$]+)\s*=\s*new URL\(window.location.href\)\.origin', mask(source))
        if prod:
            flag, origin = map(re.escape, prod.groups())
            service = r'serviceHost\s*:\s*' + flag + r'\s*\?\s*([\x27"])http://localhost/api/v1\1\s*:\s*' + origin + r'\s*\+\s*([\x27"])/api/v1\2'
            if re.search(service, source):
                parsed = urlsplit(page_url)
                base = host or f'{parsed.scheme}://{parsed.netloc}'
                prefixes.add(base.rstrip('/') + '/api/v1')
    if len(prefixes) != 1:
        raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', 'API 前缀缺失或不唯一，请使用 site_profiles')
    endpoint = prefixes.pop().rstrip('/') + '/passport/auth/login'
    # 此主题的 fetch 拦截器将 /api/* 转发到 defaultConfig.apiUrl。
    interceptor = any(re.search(r'url\s*=\s*window\.defaultConfig\.apiUrl\s*\+\s*urlObj\.pathname', mask(s)) for s in sources)
    if interceptor:
        api = _literal_field(defaults, 'apiUrl')
        if not api:
            raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', 'fetch 拦截器缺少明确 apiUrl')
        endpoint = urljoin(page_url, api.rstrip('/') + urlsplit(endpoint).path)
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise V2BoardIssue('V2BOARD_CONFIG_UNRESOLVED', 'API 地址格式错误')
    if not same_origin(page_url, endpoint):
        allowed = config.get('v2board_api_origins', [])
        exact = any(same_origin(origin, endpoint) and urlsplit(origin).path in ('', '/') for origin in allowed)
        pattern = api_origin_pattern_matches(endpoint, config.get('v2board_api_origin_patterns', []))
        if not exact and not pattern:
            raise V2BoardIssue('V2BOARD_CROSS_ORIGIN', f'识别到跨域登录接口 {endpoint}；需配置 v2board_api_origins 或 v2board_api_origin_patterns')
    return endpoint


def response_state(res):
    """仅识别结构化凭据/参数错误及限流；其余 5xx 仍作为服务端故障。"""
    if 'json' not in res.headers.get('Content-Type', '').lower():
        return None
    try:
        body = res.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    message = body.get('message', '')
    if not isinstance(message, str):
        return None
    text = message.casefold()
    if any(word in text for word in ('too many password errors', '密码错误次数过多', '密码错误过多', '分钟后再试', 'try again after')):
        return 'stopped'
    if res.status_code in (400, 422) and isinstance(body.get('errors'), dict) and set(body['errors']) & {'email', 'password'}:
        return 'failure'
    if res.status_code in (400, 401, 422, 500) and any(word in text for word in (
        'incorrect email or password', '邮箱或密码错误', '邮箱或者密码错误', 'email format is incorrect',
        'password must be greater than 8', 'your account has been suspended', '账号已被封禁')):
        return 'failure'
    return None
