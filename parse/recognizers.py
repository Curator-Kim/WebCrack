"""Conservative static JavaScript subset. No eval, no JavaScript execution."""
from dataclasses import dataclass, field
import re
from urllib.parse import urljoin, urlsplit


@dataclass
class Candidate:
    endpoint: str
    encoding: str
    username: str
    password: str
    data: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)

    @property
    def key(self):
        return self.endpoint, self.encoding, self.username, self.password, tuple(sorted((key,type(value).__name__,repr(value)) for key,value in self.data.items()))


def mask(source):
    """Mask comments, strings and expression-position regex literals; preserve offsets."""
    out = list(source)
    i = 0
    while i < len(source):
        if source.startswith('//', i) or source.startswith('/*', i):
            end = source.find('\n', i + 2) if source[i:i+2] == '//' else source.find('*/', i + 2)
            end = len(source) if end < 0 else end + (2 if source[i:i+2] == '/*' else 0)
            out[i:end] = ' ' * (end-i)
            i = end
        elif source[i] == '/' and re.search(
                r'(?:^|[=(:,\[!&|?{};])\s*$|\b(?:return|case|throw|yield|typeof|void|delete|in|of|instanceof|await)\s*$',
                source[max(0, i - 160):i]):
            # 正则字符类可包含引号、斜杠和括号；这些都不是 JS 字符串/注释。
            j, in_class = i + 1, False
            end = None
            while j < len(source) and source[j] not in '\r\n':
                char = source[j]
                if char == '\\':
                    j += 2
                    continue
                if char == '[':
                    in_class = True
                elif char == ']':
                    in_class = False
                elif char == '/' and not in_class:
                    j += 1
                    while j < len(source) and source[j].isalpha():
                        j += 1
                    end = j
                    break
                j += 1
            if end is not None:
                out[i:end] = ' ' * (end - i)
                i = end
            else:
                i += 1  # 未闭合的字面量不扩展到下一行。
        elif source[i] in "'\"`":
            quote = source[i]
            j = i + 1
            while j < len(source):
                if source[j] == '\\':
                    j += 2
                elif source[j] == quote:
                    j += 1
                    break
                else:
                    j += 1
            out[i:j] = ' ' * (j-i)
            i = j
        else:
            i += 1
    return ''.join(out)


def split_top(expression, separator=','):
    masked = mask(expression)
    depth = 0
    start = 0
    result = []
    for i, char in enumerate(masked):
        if char in '([{': depth += 1
        elif char in ')]}': depth -= 1
        elif char == separator and depth == 0:
            result.append(expression[start:i].strip())
            start = i + 1
    result.append(expression[start:].strip())
    return result


def calls(source, pattern):
    masked = mask(source)
    for match in re.finditer(pattern + r'\s*\(', masked):
        start = match.end()
        depth = 1
        for end in range(start, len(masked)):
            char = masked[end]
            if char == '(': depth += 1
            elif char == ')': depth -= 1
            if depth == 0:
                yield match, split_top(source[start:end])
                break


def object_fields(expression):
    expression = expression.strip()
    if not expression.startswith('{') or not expression.endswith('}'):
        return None
    result = {}
    for member in split_top(expression[1:-1]):
        if not member: continue
        pair = split_top(member, ':')
        if len(pair) == 1 and re.fullmatch(r'[\w$]+', pair[0]):
            result[pair[0]] = pair[0]
        elif len(pair) == 2 and re.fullmatch(r"[\w$]+|'[^']+'|\"[^\"]+\"", pair[0]):
            result[pair[0].strip("'\"")] = pair[1]
        else:
            return None
    return result


def string_value(expression, constants):
    expression = expression.strip()
    if expression in constants:
        return constants[expression]
    pieces = split_top(expression, '+')
    if len(pieces) > 1:
        values = [string_value(piece, constants) for piece in pieces]
        return ''.join(values) if all(value is not None for value in values) else None
    if len(expression) >= 2 and expression[0] == expression[-1] and expression[0] in "'\"`":
        inner = expression[1:-1]
        if expression[0] == '`':
            failed = False
            def replace(match):
                nonlocal failed
                value = constants.get(match.group(1).strip())
                if value is None: failed = True
                return value or ''
            inner = re.sub(r'\$\{([^}]+)\}', replace, inner)
            if failed or '${' in inner: return None
        # Only simple escaped literals; unresolved JS escapes are intentionally rejected.
        if re.search(r'\\(?![\\\'"`nrt/])', inner): return None
        return re.sub(r'\\(.)', lambda m: {'n':'\n','r':'\r','t':'\t'}.get(m[1],m[1]), inner)
    return None


def environment(scripts):
    constants, objects, conflicts = {}, {}, set()
    # Parse literal declarations, preserving nested values through balanced splitting.
    assignments = []
    for source in scripts:
        source_mask = mask(source)
        for match in re.finditer(r'\b(?:const|let|var)\s+([\w$]+)\s*=', source_mask):
            start = match.end()
            masked = source_mask[start:start+8192]
            depth = 0
            end = len(masked)
            for i, char in enumerate(masked):
                if char in '([{': depth += 1
                elif char in ')]}':
                    if depth == 0:
                        end = i
                        break
                    depth -= 1
                elif char in ',;' and depth == 0:
                    end = i
                    break
            expression = source[start:start+end].strip()
            assignments.append((match[1], expression))
    for _ in range(3):
        for name, expression in assignments:
            fields = object_fields(expression)
            if fields is not None:
                if name in objects and objects[name] != fields: conflicts.add(name)
                objects[name] = fields
                for key, value in fields.items():
                    resolved = string_value(value, constants)
                    if resolved is not None: constants[name+'.'+key] = resolved
            else:
                value = string_value(expression, constants)
                if value is not None:
                    if name in constants and constants[name] != value: conflicts.add(name)
                    constants[name] = value
    for name in conflicts:
        constants.pop(name, None)
        objects.pop(name, None)
        for key in list(constants):
            if key.startswith(name+'.'): constants.pop(key)
    return constants, objects


def same_origin(first, second):
    a, b = urlsplit(first), urlsplit(second)
    def origin(url):
        return url.scheme.lower(), url.hostname, url.port or (443 if url.scheme == 'https' else 80)
    try:
        return origin(a) == origin(b) and b.scheme in ('http', 'https') and not b.username and not b.password
    except ValueError:
        return False


def credentials(fields, config):
    if fields is None: return None
    usernames = [key for key in fields if key.lower() in config['username_field_names']]
    passwords = [key for key in fields if key.lower() in config['password_field_names']]
    if len(usernames) == len(passwords) == 1 and usernames[0] != passwords[0]:
        return usernames[0], passwords[0]
    return None


def payload(expression, objects):
    return objects.get(expression.strip()) or object_fields(expression)


def login_path(path):
    # Match complete path segments, never logout/register/password-reset APIs.
    return any(re.fullmatch(r'(?:login|signin|sign-in|sign_in|authenticate|authentication)(?:\.[\w]+)?', part, re.I)
               for part in urlsplit(path).path.split('/'))


def make_candidate(url_expression, fields, encoding, source_name, page_url, constants, config, prefix=''):
    path = string_value(url_expression, constants)
    names = credentials(fields, config)
    if path is None or not names or not login_path(path): return None
    if prefix and not urlsplit(path).scheme and not path.startswith('//'):
        path = prefix.rstrip('/') + '/' + path.lstrip('/')
    endpoint = urljoin(page_url, path)
    if not same_origin(page_url, endpoint): return None
    # Preserve only known scalar extra values; silently discarding dynamic fields would break requests.
    for key in names:
        value = fields[key].strip()
        if not (re.fullmatch(r"[\w$.]+|''|\"\"",value) or
                re.fullmatch(r"document\.(?:getElementById|querySelector)\([^)]*\)\.value",value)):
            return None
    data = {}
    for key, value in fields.items():
        if key in names: continue
        resolved = string_value(value, constants)
        if resolved is None:
            if value in ('true','false','null'): resolved = {'true':True,'false':False,'null':None}[value]
            elif re.fullmatch(r'-?\d+', value): resolved = int(value)
            else: return None
        data[key] = resolved
    return Candidate(endpoint, encoding, *names, data, [source_name])


def recognize_fetch(source, page_url, constants, objects, config):
    for _, args in calls(source, r'\bfetch'):
        if len(args) < 2: continue
        options = payload(args[1], objects)
        if not options or (string_value(options.get('method',''), constants) or '').upper() != 'POST': continue
        body = options.get('body','')
        if not body.startswith('JSON.stringify'): continue
        bodies = list(calls(body, r'JSON\.stringify'))
        if not bodies or not bodies[0][1]: continue
        headers = payload(options.get('headers',''), objects) or {}
        if any(key.lower() != 'content-type' for key in headers): continue
        content = string_value(headers.get('Content-Type',headers.get('content-type','')), constants)
        if content is None or 'application/json' not in content.lower(): continue
        candidate = make_candidate(args[0], payload(bodies[0][1][0], objects), 'json', 'fetch', page_url, constants, config)
        if candidate: yield candidate


def recognize_axios(source, page_url, constants, objects, config):
    clients = {'axios': ''}
    for match, args in calls(source, r'\b[\w$]+\.create'):
        preceding = source[max(0,match.start()-100):match.start()]
        assigned = re.search(r'([\w$]+)\s*=\s*$', preceding)
        options = payload(args[0], objects) if args else None
        prefix = string_value((options or {}).get('baseURL',''), constants)
        if assigned and prefix is not None:
            clients[assigned[1]] = prefix
    for client, prefix in clients.items():
        for match, args in calls(source, r'(?<![\w$.])' + re.escape(client) + r'\.(?:post|request)'):
            name = source[match.start():match.end()].split('(')[0].strip()
            if name.endswith('.post'):
                if len(args) < 2: continue
                fields = payload(args[1], objects)
                expression = args[0]
                options = payload(args[2], objects) if len(args) > 2 else {}
            else:
                options = payload(args[0], objects) if args else None
                if not options or (string_value(options.get('method',''),constants) or '').upper() != 'POST': continue
                fields, expression = payload(options.get('data',''),objects), options.get('url','')
            if options and 'headers' in options:
                headers = payload(options['headers'],objects)
                if headers is None or any(key.lower() != 'content-type' for key in headers): continue
                expression_content = next((value for key,value in headers.items() if key.lower() == 'content-type'),None)
                if expression_content is not None:
                    content = string_value(expression_content,constants)
                    if not content or 'application/json' not in content.lower(): continue
            candidate = make_candidate(expression, fields, 'json', 'axios', page_url, constants, config, prefix)
            if candidate: yield candidate
        # Callable axios({method, url, data}), including configured instances.
        for _, args in calls(source, r'(?<![\w$.])' + re.escape(client)):
            options = payload(args[0],objects) if args else None
            if not options or (string_value(options.get('method',''),constants) or '').upper() != 'POST': continue
            if 'headers' in options:
                headers = payload(options['headers'],objects)
                if headers is None or any(key.lower() != 'content-type' for key in headers): continue
                content_expression = next((value for key,value in headers.items() if key.lower() == 'content-type'),None)
                if content_expression is not None:
                    content = string_value(content_expression,constants)
                    if not content or 'application/json' not in content.lower(): continue
            candidate = make_candidate(options.get('url',''),payload(options.get('data',''),objects),'json','axios-options',page_url,constants,config,prefix)
            if candidate: yield candidate


def recognize_jquery(source, page_url, constants, objects, config):
    for _, args in calls(source, r'(?:\$|jQuery)\.ajax'):
        options = payload(args[0],objects) if args else None
        if not options: continue
        method = string_value(options.get('method', options.get('type','')),constants)
        if not method or method.upper() != 'POST' or 'headers' in options: continue
        expression = options.get('data','')
        encoding = 'form'
        if expression.startswith('JSON.stringify'):
            bodies = list(calls(expression,r'JSON\.stringify'))
            if not bodies or not bodies[0][1]: continue
            expression = bodies[0][1][0]
            encoding = 'json'
            content = string_value(options.get('contentType',''),constants)
            if not content or 'application/json' not in content.lower(): continue
        candidate = make_candidate(options.get('url',''),payload(expression,objects),encoding,'jquery-ajax',page_url,constants,config)
        if candidate: yield candidate


# 静态回调中可安全推断的成功字段名；仅这些字段参与规则提取，避免把无关条件当成成功。
SUCCESS_FIELD_NAMES = (
    "success", "succeeded", "authenticated", "isauthenticated", "is_authenticated",
    "islogin", "is_login", "isloggedin", "loggedin", "logged_in", "login",
    "ok", "code", "status", "statuscode", "status_code", "state",
    "statecode", "flag", "result", "ret",
)

_LITERALS = r"(true|false|null|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")"


def _literal_value(token):
    lowered = token.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if token[:1] in ("'", '"') and token[-1] == token[:1]:
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        try:
            return float(token)
        except ValueError:
            return None


def success_rules(sources):
    """从静态回调中提取显式成功条件，用于严格类型比较的响应判定。

    只采纳 `if (x.<成功字段>)`、`if (!x.<成功字段>)` 与
    `if (x.<成功字段> === <字面量>)`（含 `==`/`!==`/`!=`）形式，
    字段路径最多三段，返回 `{路径: [值, ...]}`。不执行 JS。
    """
    names = {name.casefold() for name in SUCCESS_FIELD_NAMES}
    rules = {}

    def accept(expression):
        parts = [part.strip() for part in expression.split(".") if part.strip()]
        # 首段是响应变量名（data/res/result...），规则路径相对 JSON 响应体。
        if len(parts) < 2 or len(parts) > 4 or not all(re.fullmatch(r"[\w$]+", part) for part in parts):
            return None
        parts = parts[1:]
        path = ".".join(parts)
        if parts[-1].casefold() not in names and path.casefold() not in names:
            return None
        return path

    def add(path, value):
        bucket = rules.setdefault(path, [])
        if value not in bucket:
            bucket.append(value)

    path_pattern = r"([\w$]+(?:\s*\.\s*[\w$]+){1,3})"
    for source in sources:
        masked = mask(source)
        for match in re.finditer(r"if\s*\(\s*!\s*" + path_pattern + r"\s*\)", masked):
            path = accept(match.group(1))
            if path:
                add(path, False)
        for match in re.finditer(r"if\s*\(\s*(?<![!=\w.])" + path_pattern + r"\s*\)", masked):
            path = accept(match.group(1))
            if path:
                add(path, True)
        for match in re.finditer(r"if\s*\(\s*" + path_pattern + r"\s*(===|==|!==|!=)", masked):
            path = accept(match.group(1))
            if not path:
                continue
            literal = re.match(r"\s*" + _LITERALS, source[match.end():match.end() + 48])
            if not literal:
                continue
            value = _literal_value(literal.group(1))
            if match.group(2) in ("!=", "!=="):
                # 反向比较（含 `!== false`）不是可靠的成功条件，保守跳过。
                continue
            add(path, value)
    return rules


RECOGNIZERS = (recognize_fetch, recognize_axios, recognize_jquery)


def discover_candidates(scripts, page_url, config):
    constants, objects = environment(scripts)
    candidates = {}
    for source in scripts:
        for recognizer in RECOGNIZERS:
            for candidate in recognizer(source,page_url,constants,objects,config):
                if candidate.key in candidates:
                    candidates[candidate.key].evidence.extend(candidate.evidence)
                else:
                    candidates[candidate.key] = candidate
    return list(candidates.values())


MISSING = object()


def json_path(body, path):
    value = body
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value: return MISSING
        value = value[part]
    return value
