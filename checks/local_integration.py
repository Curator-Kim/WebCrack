"""Run: python3 -m checks.local_integration. Makes real loopback-only HTTP requests."""
import contextlib
import csv
import io
import json
import os
import socket
from urllib.parse import urlsplit
from pathlib import Path
from unittest.mock import patch

import requests
import webcrack
from conf.config import crackConfig, generatorConfig
from checks.local_sites import ACCOUNTS, running_sites


def _main():
    output = Path(__file__).parent / 'local_results'
    output.mkdir(exist_ok=True)
    report = {'network_scope': '127.0.0.1 only', 'accounts': ACCOUNTS, 'checks': []}
    transcript = io.StringIO()
    with running_sites() as (sites, events):
        # Both sites accept the known fixture password and establish a real session.
        for kind, base in sites.items():
            username, password = ACCOUNTS[kind]
            for supplied, expected in [('incorrect-fixture', False), (password, True)]:
                with requests.Session() as session:
                    endpoint = '/session' if kind == 'html' else '/api/login'
                    kwargs = {'data' if kind == 'html' else 'json': {'username': username, 'password': supplied}}
                    res = session.post(base + endpoint, timeout=3, **kwargs)
                    protected = session.get(base + '/dashboard', timeout=3, allow_redirects=False)
                    actual = protected.status_code == 200 and 'FIXTURE_AUTHENTICATED' in protected.text
                    assert actual == expected, (kind, supplied, protected.status_code)
                    report['checks'].append({'site': kind, 'case': 'correct' if expected else 'incorrect',
                        'authenticated': actual, 'login_status': res.status_code,
                        'protected_status': protected.status_code})
        urls = output / 'urls.txt'
        urls.write_text('\n'.join(base + '/login' for base in sites.values()) + '\n')
        base_dict = generatorConfig['dict_config']['base_dict']
        with patch.dict(crackConfig, {'success_words': ['FIXTURE_AUTHENTICATED'],
                                     'json_success_fields': {'authenticated': True}, 'delay': 0,
                                     'timeout': 3, 'requests_proxies': {}}), \
             patch.dict(base_dict, {'username_list': ['admin'], 'password_list': ['incorrect-fixture', '123456', 'admin123']}), \
             patch.dict(generatorConfig['dict_config']['domain_dict'], {'enable': False}), \
             patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
             patch.dict(generatorConfig['headers_config'], {'enable': False}), \
             contextlib.redirect_stdout(transcript):
            exit_code = webcrack.main(['-f', str(urls), '-o', str(output / 'output.txt')])
        with (output / 'output.txt').open(newline='') as handle:
            results = list(csv.DictReader(handle, delimiter='\t'))
        assert exit_code == 0
        assert results == [{'url': sites['html'] + '/login', 'username': 'admin', 'password': '123456'},
                           {'url': sites['ajax'] + '/login', 'username': 'admin', 'password': 'admin123'}], results
        assert 'No form found' not in transcript.getvalue()
        # Exercise actual JSON responses through the classifier separately from discovery.
        from crack.crack_task import CrackTask, LoginState
        from types import SimpleNamespace
        with patch.dict(crackConfig, {'success_words': [], 'json_success_fields': {'authenticated': True}}):
            task = CrackTask()
            task.parser = SimpleNamespace(cms={}, username_keyword='username', password_keyword='password')
            for supplied, expected in [('incorrect-fixture', LoginState.FAILURE), ('admin123', LoginState.SUCCESS)]:
                res = requests.post(sites['ajax'] + '/api/login', json={'username': 'admin', 'password': supplied}, timeout=3)
                state = task.classify_response(res)
                assert state == expected, state
                report['checks'].append({'site': 'ajax', 'case': 'JSON classification', 'state': state.value})
        report.update({'exit_status': exit_code, 'detected_results': results,
                       'html_end_to_end': 'PASS', 'ajax_end_to_end': 'PASS',
                       'ajax_json_classifier': 'PASS', 'events': events})
    for base in sites.values():
        parsed = urlsplit(base)
        with socket.socket() as client:
            client.settimeout(1)
            assert client.connect_ex((parsed.hostname, parsed.port)) != 0, 'Server still listening'
    report['servers_stopped'] = True
    (output / 'transcript.txt').write_text(transcript.getvalue())
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('HTML form: correct/incorrect login + WebCrack end-to-end PASS')
    print('AJAX JSON: correct/incorrect login + classifier + WebCrack end-to-end PASS')
    print('Servers stopped; report:', (output / 'report.json').resolve())
    return 0


def main():
    with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}):
        return _main()


if __name__ == '__main__':
    raise SystemExit(main())
