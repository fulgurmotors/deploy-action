#!/usr/bin/env python3
from pathlib import Path
import sys
root = Path(__file__).resolve().parent.parent
client = (root / 'scripts/deploy.py').read_text().removeprefix('#!/usr/bin/env python3\n')
workflow = '''name: Déployer avec deploy-hub
on:
  workflow_call: {}
permissions:
  contents: read
  id-token: write
concurrency:
  group: deploy-${{ github.repository }}
  cancel-in-progress: true
jobs:
  deploy:
    runs-on: ubuntu-24.04
    timeout-minutes: 42
    steps:
      - name: Récupérer le commit
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with:
          ref: ${{ github.sha }}
          fetch-depth: 1
          persist-credentials: false
      - name: Envoyer le code et suivre le déploiement
        shell: bash
        run: |
          python3 -I - <<'DEPLOY_HUB_CLIENT'
'''
workflow += ''.join('          ' + line + '\n' for line in client.splitlines())
workflow += '          DEPLOY_HUB_CLIENT\n'
target = root / '.github/workflows/deploy.yml'
if '--check' in sys.argv:
    if target.read_text() != workflow:
        raise SystemExit('Regénérer .github/workflows/deploy.yml avec scripts/render-workflow.py')
else:
    target.write_text(workflow)
