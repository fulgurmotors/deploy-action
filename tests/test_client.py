import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deploy_client', ROOT / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.lines = []; self.client = deploy.Client(emit=self.lines.append)
    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        env = os.environ | {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.test', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.test'}
        return subprocess.check_output(['git', '-C', str(self.root), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()

    def test_archive_is_exact_commit_without_git_untracked_or_credentials(self):
        self.git('init', '--initial-branch=main')
        (self.root / 'app.js').write_text('console.log("test")'); self.git('add', 'app.js'); self.git('commit', '-m', 'initial')
        (self.root / '.env').write_text('secret-not-tracked')
        sha = self.git('rev-parse', 'HEAD'); path = self.root / 'source.tar.gz'
        size = self.client.archive(self.root, sha, path)
        self.assertEqual(size, path.stat().st_size)
        with tarfile.open(path) as tar:
            self.assertEqual(tar.getnames(), ['app.js'])
        with self.assertRaises(deploy.Failure):
            self.client.archive(self.root, 'b' * 40, self.root / 'wrong.tar.gz')

    def test_compressed_limit_cuts_generation_before_upload(self):
        self.git('init', '--initial-branch=main'); (self.root / 'large.bin').write_bytes(os.urandom(4096))
        self.git('add', 'large.bin'); self.git('commit', '-m', 'random fixture')
        with patch.object(deploy, 'MAX_ARCHIVE', 1024), self.assertRaisesRegex(deploy.Failure, '200 Mio'):
            self.client.archive(self.root, self.git('rev-parse', 'HEAD'), self.root / 'too-big.tar.gz')

    def test_oidc_audience_and_permission_failure_without_token_output(self):
        seen = []
        def fetch(request):
            seen.append(request); return {'value': 'synthetic.oidc.token'}
        self.client.request_json = fetch
        token = self.client.oidc_token({'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://runner.example.test/token?api=2&audience=old', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-request-token'})
        self.assertEqual(token, 'synthetic.oidc.token')
        query = deploy.urllib.parse.parse_qs(deploy.urllib.parse.urlsplit(seen[0].full_url).query)
        self.assertEqual(query['audience'], ['deploy.flgr.fr']); self.assertEqual(query['api'], ['2'])
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer synthetic-request-token'); self.assertEqual(self.lines, [])
        with self.assertRaisesRegex(deploy.Failure, 'id-token'):
            self.client.oidc_token({})
        with self.assertRaises(deploy.Failure):
            self.client.oidc_token({'ACTIONS_ID_TOKEN_REQUEST_URL': 'http://runner.example.test/token', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-request-token'})

    def test_poll_prints_only_new_log_and_success(self):
        ticks = [0]; self.client.clock = lambda: ticks[0]; self.client.sleep = lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
        records = iter([{'state': 'started', 'log': 'build\n', 'offset': 0}, {'state': 'started', 'log': 'build\n', 'offset': 0}, {'state': 'ok', 'log': 'build\nready\n', 'offset': 0}])
        self.client.request_json = lambda request: next(records)
        self.client.follow({'id': 'a' * 32, 'read_token': 'b' * 43})
        self.assertEqual(sum('build' in line for line in self.lines), 1); self.assertIn('ready', '\n'.join(self.lines)); self.assertEqual(ticks[0], 10)

    def test_failed_and_superseded_jobs_fail_with_logs(self):
        for state in ('failed', 'superseded'):
            self.client.request_json = lambda request: {'state': state, 'log': 'controlled failure\n', 'offset': 0}
            with self.assertRaises(deploy.Failure):
                self.client.follow({'id': 'a' * 32, 'read_token': 'b' * 43})
        self.assertIn('controlled failure', '\n'.join(self.lines))

    def test_poll_timeout_has_no_second_upload(self):
        now = [0]; self.client.clock = lambda: now[0]; self.client.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        self.client.request_json = lambda request: {'state': 'started', 'log': '', 'offset': 0}
        with self.assertRaisesRegex(deploy.Failure, '30 minutes'):
            self.client.follow({'id': 'a' * 32, 'read_token': 'b' * 43})
        self.assertEqual(now[0], 1800)

    def test_truncated_log_offsets_bad_state_and_secret_masking(self):
        self.client.request_json = lambda request: {'state': 'ok', 'log': 'tail', 'offset': 9000}
        self.client.follow({'id': 'a' * 32, 'read_token': 'b' * 43})
        self.assertIn('tampon', self.lines[0])
        self.client.remember('synthetic-private-token'); self.client.message('::error::untrusted\nAuthorization: Bearer synthetic-private-token\nvalue=synthetic-private-token')
        output = '\n'.join(self.lines); self.assertNotIn('synthetic-private-token', output)
        self.assertTrue(all(line.startswith('[déploiement] ') for line in self.lines))
        self.client.request_json = lambda request: {'state': 'nonsense', 'log': '', 'offset': 0}
        with self.assertRaises(deploy.Failure):
            self.client.follow({'id': 'a' * 32, 'read_token': 'b' * 43})

    def test_post_has_one_attempt_and_no_token_in_argv(self):
        token = 'synthetic-oidc-token'; path = self.root / 'archive'; path.write_bytes(b'fixture'); response = self.root / 'response'
        calls = []
        def fake_curl(command, **kwargs):
            calls.append((command, kwargs)); response.write_text(json.dumps({'id': 'a' * 32, 'read_token': 'b' * 43, 'state': 'queued'}))
            return subprocess.CompletedProcess(command, 0, '202', '')
        with patch.object(deploy.subprocess, 'run', side_effect=fake_curl):
            result = self.client.upload(path, token, 'a' * 40, response)
        self.assertEqual(result['state'], 'queued'); self.assertEqual(len(calls), 1)
        args, options = calls[0]; self.assertNotIn(token, ' '.join(args)); self.assertIn(token, options['input'])
        self.assertNotIn('--retry', args); self.assertNotIn('--location', args); self.assertIn('Expect: 100-continue', args)
        with patch.object(deploy.subprocess, 'run', return_value=subprocess.CompletedProcess(args, 56, '000', 'private diagnostic')) as run:
            with self.assertRaisesRegex(deploy.Failure, 'aucun nouvel envoi'):
                self.client.upload(path, token, 'a' * 40, response)
            self.assertEqual(run.call_count, 1)

    def test_event_is_checked_before_archive_or_token(self):
        with patch.object(self.client, 'archive') as archive, patch.object(self.client, 'oidc_token') as token:
            with self.assertRaises(deploy.Failure):
                self.client.run({'GITHUB_EVENT_NAME': 'pull_request'})
            archive.assert_not_called(); token.assert_not_called()

    def test_json_errors_and_redirects_do_not_follow_other_host(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise urllib.error.HTTPError('https://example.test', 404, 'not found', {}, io.BytesIO(b'{"message":"pas branche"}'))
        with patch.object(deploy.urllib.request, 'build_opener', return_value=Opener()), self.assertRaisesRegex(deploy.Failure, 'pas branche'):
            self.client.request_json(deploy.urllib.request.Request('https://example.test'))
        with self.assertRaises(deploy.Failure):
            deploy.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.test')

    def test_rendered_workflow_is_current_and_python_is_isolated(self):
        subprocess.run([deploy.os.sys.executable, str(ROOT / 'scripts/render-workflow.py'), '--check'], check=True)
        flow = (ROOT / '.github/workflows/deploy.yml').read_text()
        self.assertIn('workflow_call:', flow); self.assertIn('python3 -I -', flow)
        self.assertIn('persist-credentials: false', flow); self.assertIn('cancel-in-progress: true', flow)
        # Execute that exact embedded client from a hostile caller checkout.
        (self.root / 'json.py').write_text('raise RuntimeError("CALLER CODE MUST NOT RUN")')
        client_source = flow.split("          python3 -I - <<'DEPLOY_HUB_CLIENT'\n", 1)[1].rsplit('          DEPLOY_HUB_CLIENT', 1)[0]
        client_source = '\n'.join(line[10:] for line in client_source.splitlines())
        env = os.environ | {'GITHUB_EVENT_NAME': 'pull_request'}
        result = subprocess.run([deploy.os.sys.executable, '-I', '-'], input=client_source, cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1); self.assertNotIn('CALLER CODE', result.stderr); self.assertIn('push', result.stdout)


class LiveCurlTests(unittest.TestCase):
    """Synthetic TLS server only; no system trust or network settings change."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        config = cls.root / 'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1\n')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-config', str(config), '-keyout', str(cls.root / 'test.key'), '-out', str(cls.root / 'test.crt')], check=True, capture_output=True)
        cls.received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def handle_expect_100(self):
                return True
            def log_message(self, *args):
                pass
            def do_POST(self):
                if self.path.startswith('/unlinked/'):
                    data = json.dumps({'message': 'Ce dépôt n’est branché sur aucune machine : demande à un admin ou branche-le dans le panel.'}).encode(); code = 404
                else:
                    self.send_response_only(100); self.end_headers()
                    cls.received.append(self.rfile.read(int(self.headers['Content-Length'])))
                    data = json.dumps({'id': 'a' * 32, 'read_token': 'b' * 43, 'state': 'queued'}).encode(); code = 202
                self.send_response(code); self.send_header('Content-Length', str(len(data))); self.send_header('Connection', 'close'); self.end_headers(); self.wfile.write(data); self.close_connection = True
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cls.root / 'test.crt', cls.root / 'test.key')
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def test_https_archive_and_early_unlinked_reply(self):
        path = self.root / 'source.tar.gz'; data = b'synthetic archive bytes' * 100000; path.write_bytes(data)
        client = deploy.Client(emit=lambda text: None); token = client.remember('synthetic-oidc-token')
        with patch.dict(os.environ, {'CURL_CA_BUNDLE': str(self.root / 'test.crt'), 'NO_PROXY': '127.0.0.1'}):
            with patch.object(deploy, 'ENDPOINT', 'https://127.0.0.1:' + str(self.server.server_port)):
                response = client.upload(path, token, 'a' * 40, self.root / 'response.json')
                self.assertEqual(response['state'], 'queued'); self.assertEqual(self.received[-1], data)
            before = len(self.received)
            with patch.object(deploy, 'ENDPOINT', 'https://127.0.0.1:' + str(self.server.server_port) + '/unlinked'), self.assertRaisesRegex(deploy.Failure, 'aucune machine'):
                client.upload(path, token, 'a' * 40, self.root / 'response.json')
            self.assertEqual(len(self.received), before)
