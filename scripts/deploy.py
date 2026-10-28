#!/usr/bin/env python3
"""Client embedded in the reusable workflow. No repository code is executed."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'https://deploy.flgr.fr'
AUDIENCE = 'deploy.flgr.fr'
MAX_ARCHIVE = 200 * 1024**2
MAX_RESPONSE = 262144
FINAL_STATES = {'ok', 'failed', 'superseded'}
STATES = FINAL_STATES | {'queued', 'started'}


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('Redirection HTTP refusée.')


class Client:
    def __init__(self, emit=print, clock=time.monotonic, sleep=time.sleep):
        self.emit, self.clock, self.sleep = emit, clock, sleep
        self.secrets = []

    def clean(self, text):
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, '[masqué]')
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]', '', text)
        return re.sub(r'(?im)^.*(?:Authorization\s*:|Bearer\s+|Cookie\s*:|Set-Cookie\s*:).*$', '[autorisation masquée]', text)

    def message(self, text):
        for line in self.clean(text).splitlines():
            # Prefix prevents application logs from becoming Actions commands.
            self.emit('[déploiement] ' + line)

    def remember(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise Failure('Jeton reçu invalide.')
        self.secrets.append(token)
        return token

    def request_json(self, request):
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(MAX_RESPONSE + 1)
                value = json.loads(raw) if len(raw) <= MAX_RESPONSE else {}
            except (ValueError, OSError):
                value = {}
            message = value.get('message') if isinstance(value, dict) else None
            raise Failure(message if isinstance(message, str) else 'Requête refusée, HTTP ' + str(error.code) + '.')
        except (OSError, urllib.error.URLError):
            raise Failure('Connexion indisponible ; aucun nouvel envoi automatique.')
        if len(raw) > MAX_RESPONSE:
            raise Failure('Réponse du serveur trop grande.')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise Failure('Réponse JSON illisible.')
        if not isinstance(result, dict):
            raise Failure('Réponse JSON inattendue.')
        return result

    def oidc_token(self, environ):
        url = environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
        request_token = environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
        if not url or not request_token:
            raise Failure('Permission id-token: write requise dans le workflow appelant.')
        self.remember(request_token)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise Failure('Adresse OIDC du runner invalide.')
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if key != 'audience']
        query.append(('audience', AUDIENCE))
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        result = self.request_json(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + request_token}))
        return self.remember(result.get('value'))

    def archive(self, folder, commit, target):
        if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
            raise Failure('SHA Git complet manquant ou invalide.')
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=folder, capture_output=True, text=True, check=True).stdout.strip()
        if head != commit:
            raise Failure('Le checkout ne correspond pas au commit GitHub ; envoi refusé.')
        with tempfile.TemporaryFile() as errors, Path(target).open('xb') as output:
            process = subprocess.Popen(['git', 'archive', '--format=tar.gz', 'HEAD'], cwd=folder, stdout=subprocess.PIPE, stderr=errors)
            count = 0
            try:
                while block := process.stdout.read(65536):
                    count += len(block)
                    if count > MAX_ARCHIVE:
                        raise Failure('Archive supérieure à 200 Mio compressés ; aucun envoi effectué.')
                    output.write(block)
                if process.wait(timeout=10):
                    raise Failure('git archive a échoué ; aucun envoi effectué.')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stdout.close()
        return count

    def upload(self, archive, token, commit, response_path):
        # curl handles Expect:100 and an early 404 while streaming a large file.
        # Credentials go through stdin config, never argv, environment or a file.
        configuration = 'header = ' + json.dumps('Authorization: Bearer ' + token) + '\n'
        command = ['curl', '--disable', '--config', '-', '--silent', '--show-error',
                   '--proto', '=https', '--max-redirs', '0', '--connect-timeout', '15', '--max-time', '600',
                   '--max-filesize', str(MAX_RESPONSE), '--request', 'POST',
                   '--header', 'Content-Type: application/gzip', '--header', 'Expect: 100-continue',
                   '--header', 'X-Deploy-Sha: ' + commit, '--data-binary', '@' + str(archive),
                   '--output', str(response_path), '--write-out', '%{http_code}', ENDPOINT + '/v2/deploy']
        try:
            result = subprocess.run(command, input=configuration, capture_output=True, text=True, timeout=610)
        except (OSError, subprocess.TimeoutExpired):
            raise Failure('Transport interrompu : réception incertaine, aucun nouvel envoi automatique.')
        try:
            raw = Path(response_path).read_bytes()
            value = json.loads(raw) if len(raw) <= MAX_RESPONSE else {}
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        if result.returncode:
            raise Failure('Transport interrompu : réception incertaine, aucun nouvel envoi automatique.')
        if result.stdout.strip() != '202':
            message = value.get('message')
            raise Failure(message if isinstance(message, str) else 'Envoi refusé, HTTP ' + result.stdout.strip()[:3] + '.')
        if not re.fullmatch(r'[0-9a-f]{32}', str(value.get('id', ''))) or value.get('state') not in ('queued', 'started') or not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(value.get('read_token', ''))):
            raise Failure('Réponse de prise en charge invalide ; aucun nouvel envoi automatique.')
        self.remember(value['read_token'])
        return value

    def follow(self, submission):
        deadline, position = self.clock() + 1800, 0
        while self.clock() < deadline:
            request = urllib.request.Request(ENDPOINT + '/v2/deploys/' + submission['id'], headers={'Authorization': 'Bearer ' + submission['read_token']})
            record = self.request_json(request)
            log, offset = record.get('log'), record.get('offset')
            if record.get('state') not in STATES or not isinstance(log, str) or len(log) > 8000 or type(offset) is not int or offset < 0:
                raise Failure('Réponse de suivi invalide ; vérifie le résultat dans le panel.')
            if position < offset:
                self.message('Une partie du journal a dépassé le tampon ; suite ci-dessous.')
            delta = log[max(0, position - offset):]
            if delta:
                self.message(delta)
            position = offset + len(log)
            state = record['state']
            if state == 'ok':
                self.message('Déploiement terminé avec succès.'); return
            if state == 'failed':
                raise Failure('Le déploiement a échoué ; voir le journal ci-dessus.')
            if state == 'superseded':
                raise Failure('Cette attente a été remplacée par un push plus récent.')
            self.sleep(min(5, max(0, deadline - self.clock())))
        raise Failure('Suivi arrêté après 30 minutes : vérifie le résultat ; aucun nouvel envoi automatique.')

    def run(self, environ):
        if environ.get('GITHUB_EVENT_NAME') not in ('push', 'workflow_dispatch'):
            raise Failure('Seuls push et workflow_dispatch sont autorisés.')
        folder = Path(environ.get('GITHUB_WORKSPACE', '.'))
        commit = environ.get('GITHUB_SHA', '')
        with tempfile.TemporaryDirectory(prefix='deploy-action-') as directory:
            archive = Path(directory) / 'source.tar.gz'
            size = self.archive(folder, commit, archive)
            self.message('Archive du commit ' + commit[:7] + ' : ' + str(size) + ' octets.')
            token = self.oidc_token(environ)
            submission = self.upload(archive, token, commit, Path(directory) / 'response.json')
            self.message('Déploiement accepté ; suivi du journal.')
            self.follow(submission)


def main():
    os.umask(0o077)
    client = Client(emit=lambda line: print(line, flush=True))
    try:
        client.run(os.environ)
    except Exception:
        import sys
        error = sys.exc_info()[1]
        client.message(str(error) if isinstance(error, Failure) else 'Erreur du runner ; aucun nouvel envoi automatique.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
