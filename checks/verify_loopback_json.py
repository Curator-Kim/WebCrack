"""Known-credential test against the user-provided loopback site; no dictionary expansion."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import requests
from conf.config import crackConfig, generatorConfig
from parse.parser import Parser
import webcrack


def main():
    base = 'https://127.0.0.1:8080'
    username = os.environ.get('WEBCRACK_TEST_USERNAME', 'admin')
    password = os.environ['WEBCRACK_TEST_PASSWORD']
    out = Path(__file__).parent / 'json_results'
    out.mkdir(exist_ok=True)
    report = {'target': base, 'credentials_source': 'user-provided known fixture', 'token_saved': False}
    transcript = io.StringIO()
    with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}), \
         patch.dict(crackConfig, {'requests_proxies': {}, 'timeout': 10, 'delay': 0}), \
         patch.dict(generatorConfig['dict_config']['base_dict'], {'username_list': [username], 'password_list': [password]}), \
         patch.dict(generatorConfig['dict_config']['domain_dict'], {'enable': False}), \
         patch.dict(generatorConfig['dict_config']['sqlin_dict'], {'enable': False}), \
         contextlib.redirect_stdout(transcript):
        with requests.Session() as session:
            parser = Parser(base, session=session)
            assert parser.run(), 'Parser failed'
            assert parser.request_format == 'json'
            report['discovered_endpoint'] = parser.post_path
            report['request_format'] = parser.request_format
            wrong = session.post(parser.post_path, json={'username': 'webcrack_invalid_fixture', 'password': 'invalid_fixture_only'}, verify=False, timeout=10)
            report['wrong_credentials_status'] = wrong.status_code
            assert wrong.status_code == 401
            right = session.post(parser.post_path, json={'username': username, 'password': password}, verify=False, timeout=10)
            data = right.json()
            report['correct_credentials_status'] = right.status_code
            report['nonempty_token'] = isinstance(data.get('token'), str) and bool(data['token'].strip())
            assert right.status_code == 200 and report['nonempty_token']
            anonymous = session.get(base + '/api/auth/validate', verify=False, timeout=10)
            valid = session.get(base + '/api/auth/validate', headers={'Authorization': 'Bearer ' + data['token']}, verify=False, timeout=10)
            report['validate_without_token_status'] = anonymous.status_code
            report['validate_with_token_status'] = valid.status_code
            assert anonymous.status_code == 401 and valid.status_code == 200
            # Revoke the verification token; subsequent WebCrack token sessions expire server-side.
            logout = session.post(base + '/api/auth/logout', headers={'Authorization': 'Bearer ' + data['token']}, verify=False, timeout=10)
            report['verification_token_logout_status'] = logout.status_code
        exit_code = webcrack.main(['-u', base, '-o', str(out / 'output.txt')])
        report['webcrack_exit_status'] = exit_code
        with (out / 'output.txt').open(newline='') as handle:
            rows = list(csv.DictReader(handle, delimiter='\t'))
        assert rows == [{'url': base, 'username': username, 'password': password}], rows
        report['detected_count'] = len(rows)
    (out / 'transcript.txt').write_text(transcript.getvalue())
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
