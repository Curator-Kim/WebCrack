"""Bounded same-origin static resource discovery and per-parser cache."""
from collections import deque
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
from http_requests import TaskStopped, request_with_timeout_retries
from parse.recognizers import same_origin, mask, environment, string_value, split_top, calls


def _assignment_expression(source, start):
    """有界读取赋值右侧，保留字符串并按括号深度切分。"""
    fragment = source[start:start + 8192]
    masked = mask(fragment)
    depth = 0
    for i, char in enumerate(masked):
        if char in '([{':
            depth += 1
        elif char in ')]}':
            if depth == 0:
                return fragment[:i].strip()
            depth -= 1
        elif char in ';,\n' and depth == 0:
            return fragment[:i].strip()
    # 截断的表达式不作为可确定的值。
    return fragment.strip() if len(fragment) < 8192 and depth == 0 else None


def _script_priority(path):
    path = path.lower()
    if any(word in path for word in ('login', 'auth')):
        return 0
    if re.search(r'(?:^|/)(?:i18n|locales?|lang)(?:/|[.-])', path):
        return 3
    if any(word in path for word in ('vendor', 'polyfill')):
        return 2
    return 1


def dynamic_script_paths(sources, limit=8):
    """提取明确 script 元素的 src；不执行 JS，不扫描任意 .js 字符串。"""
    if limit <= 0:
        return []
    candidate_budget = max(64, limit * 8)
    constants, _ = environment(sources)
    arrays, conflicts = {}, set()
    for source in sources:
        masked = mask(source)
        for match in re.finditer(r'\b(?:const|let|var)\s+([\w$]+)\s*=\s*\[', masked):
            expression = _assignment_expression(source, match.end() - 1)
            if not expression or not expression.endswith(']'):
                continue
            values = [string_value(value, constants) for value in split_top(expression[1:-1]) if value]
            values = tuple(value for value in values if value is not None)
            if match[1] in arrays and arrays[match[1]] != values:
                conflicts.add(match[1])
            arrays[match[1]] = values
    for name in conflicts:
        arrays.pop(name, None)

    def paths_in(code, local):
        masked = mask(code)
        script_vars = set()
        for match in re.finditer(r"\b([\w$]+)\s*=\s*document\.createElement\(\s*(['\"])script\2\s*\)", code):
            if masked[match.start():match.start() + len(match[1])].strip():
                script_vars.add(match[1])
        for name in script_vars:
            for match in re.finditer(r'(?<![\w$.])' + re.escape(name) + r'\.src\s*=', masked):
                expression = _assignment_expression(code, match.end())
                if expression is None:
                    continue
                value = string_value(expression, local)
                if value is None:
                    # 仅对已知版本装饰形式提取原始路径；不推断其他函数的返回值。
                    wrapper = re.fullmatch(r'(?:[\w$]+\.)*getVersionedUrl\(\s*(.*?)\s*\)', expression, re.S)
                    value = string_value(wrapper[1], local) if wrapper else None
                if value and re.search(r'\.m?js(?:[?#].*)?$', value, re.I):
                    yield value

    result = []
    for source in sources:
        for value in paths_in(source, constants):
            if value not in result:
                result.append(value)
                if len(result) >= candidate_budget:
                    return sorted(result, key=_script_priority)[:limit]
        for name, values in arrays.items():
            for _, args in calls(source, r'(?<![\w$.])' + re.escape(name) + r'\.forEach'):
                if not args:
                    continue
                callback = args[0]
                parameter = re.match(r'\s*(?:\(?\s*([\w$]+)\s*\)?\s*=>|function\s*\(\s*([\w$]+)\s*\))', callback)
                if not parameter:
                    continue
                for value in values:
                    local = dict(constants)
                    local[parameter[1] or parameter[2]] = value
                    for path in paths_in(callback, local):
                        if path not in result:
                            result.append(path)
                            if len(result) >= candidate_budget:
                                return sorted(result, key=_script_priority)[:limit]
    return sorted(result, key=_script_priority)[:limit]


def load_scripts(parser, config):
    soup = BeautifulSoup(parser.resp_content, 'lxml')
    scripts = []
    maximum = config['json_script_max_bytes']
    budget = config.get('script_total_max_bytes', maximum * config['json_script_limit'])
    remaining = budget
    for tag in soup.find_all('script'):
        if tag.get('src') or tag.get('type','').lower() in ('application/json','application/ld+json'): continue
        text = tag.get_text()
        size = len(text.encode())
        if size <= maximum and size <= remaining:
            scripts.append(text)
            remaining -= size
    initial = [tag['src'] for tag in soup.find_all('script',src=True)]
    initial += [tag['href'] for tag in soup.find_all('link',href=True)
                if any(rel in ('modulepreload','preload') for rel in tag.get('rel',[]))
                and (tag.get('as') == 'script' or 'modulepreload' in tag.get('rel',[]))]
    if config.get("discover_dynamic_scripts", True):
        initial += dynamic_script_paths(scripts, limit=config["json_script_limit"])
    # Login-related resources first; module entries and other scripts follow within the limit.
    initial.sort(key=_script_priority)
    queue = deque((urljoin(parser.response_url,src),0) for src in initial)
    visited = set()
    client = parser.session if parser.session is not None else requests
    while queue and len(visited) < config['json_script_limit'] and remaining > 0:
        url, depth = queue.popleft()
        if url in visited or not same_origin(parser.response_url,url): continue
        visited.add(url)
        if url in parser.script_cache:
            source = parser.script_cache[url]
        else:
            source = None
            def download():
                # 重试范围包含流式响应体读取；每轮丢弃未完整读取的内容并关闭响应。
                with client.get(url, timeout=parser.timeout, verify=False, headers=parser.headers(),
                                proxies=parser.requests_proxies, allow_redirects=False, stream=True) as res:
                    if res.status_code != 200:
                        return None
                    chunks, total = [], 0
                    for chunk in res.iter_content(8192):
                        total += len(chunk)
                        if total > min(maximum, remaining):
                            return None
                        chunks.append(chunk)
                    return b''.join(chunks).decode('utf-8', errors='replace')
            try:
                source = request_with_timeout_retries(download, context=f"JS GET {url}",
                                                      stop_event=getattr(parser, "stop_event", None))
            except TaskStopped:
                raise
            except requests.RequestException as exc:
                parser.resource_warnings.append(f'{url}: {type(exc).__name__}')
            parser.script_cache[url] = source
        if source is None: continue
        size = len(source.encode())
        if size > remaining: continue
        remaining -= size
        scripts.append(source)
        if depth < config.get('script_dependency_depth',1):
            # Only static imports/reexports and literal import() calls, never eval or computed imports.
            regex = r"\b(?:import|export)\s+(?:[^;\n]{0,300}?\bfrom\s*)?['\"]([^'\"]+\.m?js(?:\?[^'\"]*)?)['\"]|\bimport\(\s*['\"]([^'\"]+\.m?js(?:\?[^'\"]*)?)['\"]\s*\)"
            masked = mask(source)
            dependencies = []
            for match in re.finditer(regex,source):
                if not masked[match.start():match.start()+6].strip(): continue
                dependency = match.group(1) or match.group(2)
                if dependency.startswith(('.', '/')):
                    dependencies.append((urljoin(url,dependency),depth+1))
            if config.get("discover_dynamic_scripts", True):
                dependencies += [(urljoin(url, path), depth + 1) for path in
                                 dynamic_script_paths([source], limit=config["json_script_limit"])]
            # Visit dependencies before unrelated preloads.
            queue.extendleft(reversed(dependencies))
    parser.resource_count = len(visited)
    return scripts
