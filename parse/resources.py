"""Bounded same-origin static resource discovery and per-parser cache."""
from collections import deque
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
from parse.recognizers import same_origin, mask


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
    # Login-related resources first; module entries and other scripts follow within the limit.
    initial.sort(key=lambda src: not any(word in src.lower() for word in ('login','auth')))
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
            try:
                with client.get(url,timeout=parser.timeout,verify=False,headers=parser.headers(),
                                proxies=parser.requests_proxies,allow_redirects=False,stream=True) as res:
                    if res.status_code != 200: continue
                    chunks = []; total = 0
                    for chunk in res.iter_content(8192):
                        total += len(chunk)
                        if total > min(maximum,remaining): break
                        chunks.append(chunk)
                    else:
                        source = b''.join(chunks).decode('utf-8',errors='replace')
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
            # Visit dependencies before unrelated preloads.
            queue.extendleft(reversed(dependencies))
    parser.resource_count = len(visited)
    return scripts
