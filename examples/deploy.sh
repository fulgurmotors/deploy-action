#!/usr/bin/env bash
# deploy.sh — lancé par deploy-hub après chaque push sur la branche suivie.
# À mettre à la racine du dépôt, exécutable (git add --chmod=+x deploy.sh).
#
# Quand il démarre, le code est déjà à jour dans la CT (archive ou ancien mode git).
# deploy-hub fournit : OLD_SHA (version d'avant), NEW_SHA (nouvelle), BRANCH, REPO_DIR,
# CHANGED_FILES (fichier listant les ajouts, modifications et suppressions).
# Si le script sort en erreur, le déploiement est marqué « échoué » et les
# dernières lignes s'affichent dans le panel. Il doit pouvoir être relancé sans risque.

set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Réglages du projet : la seule partie à adapter
# ---------------------------------------------------------------------------
SERVICE="${SERVICE:-$(basename "$PWD")}"   # nom de l'unité systemd (sans .service)
APP_USER="${APP_USER:-}"                   # utilisateur qui construit l'appli (vide = root)
HEALTH_URL="${HEALTH_URL-http://127.0.0.1:3000/}"  # vide = pas de vérification HTTP
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-30}                          # secondes pour que l'appli réponde
# ---------------------------------------------------------------------------

OLD_SHA="${OLD_SHA:-HEAD}"
NEW_SHA="${NEW_SHA:-HEAD}"

say()  { echo "→ $*"; }
fail() { echo "✗ $*" >&2; exit 1; }
run()  { if [ -n "$APP_USER" ]; then runuser -u "$APP_USER" -- "$@"; else "$@"; fi; }
changed() {
  [ -n "${CHANGED_FILES:-}" ] && [ -f "$CHANGED_FILES" ] || return 0
  for name in "$@"; do grep -qxF -- "$name" "$CHANGED_FILES" && return 0; done
  return 1
}

say "déploiement de ${NEW_SHA:0:7} (avant : ${OLD_SHA:0:7}) sur ${BRANCH:-?}"

# deploy-hub met le code à jour en root : on rend les fichiers à l'utilisateur de l'appli
# avant de construire sous son nom.
if [ -n "$APP_USER" ]; then
  # Les fichiers de suivi et les données locales gardent leur propriétaire.
  python3 - "$APP_USER" "${CHANGED_FILES:-}" <<'PYOWN'
import json,os,pathlib,pwd,sys
user=pwd.getpwnam(sys.argv[1]); root=pathlib.Path.cwd(); paths={root}
manifest=root/'.deploy-hub/manifest'
if manifest.is_file():
    names=json.loads(manifest.read_text())['files']
else:
    names=pathlib.Path(sys.argv[2]).read_text().splitlines() if sys.argv[2] else []
for name in names:
    relative=pathlib.PurePosixPath(name)
    if relative.is_absolute() or any(p in ('.','..') for p in relative.parts) or relative.parts[0] in ('.deploy-hub','.git','.git.avant-deploy-hub-2'):
        raise SystemExit('Chemin de suivi invalide')
    path=root/relative
    if path.exists() or path.is_symlink(): paths.add(path)
    paths.update(p for p in path.parents if p!=root and root in p.parents)
for path in sorted(paths,key=lambda p:len(p.parts)):
    if path.exists() or path.is_symlink(): os.chown(path,user.pw_uid,user.pw_gid,follow_symlinks=False)
PYOWN
fi

if [ -f compose.yaml ] || [ -f compose.yml ] || [ -f docker-compose.yml ]; then
  # --- Projet Docker Compose ---------------------------------------------
  say "docker compose : construction et redémarrage"
  docker compose up -d --build --remove-orphans
  docker compose ps

elif [ -f package.json ]; then
  # --- Projet Node.js ------------------------------------------------------
  if   [ -f pnpm-lock.yaml ]; then PM=pnpm; INSTALL=(pnpm install --frozen-lockfile)
  elif [ -f yarn.lock ];      then PM=yarn; INSTALL=(yarn install --frozen-lockfile)
  else                             PM=npm;  INSTALL=(npm ci --no-audit --no-fund)
  fi

  if [ ! -d node_modules ] || changed package.json package-lock.json pnpm-lock.yaml yarn.lock; then
    say "dépendances modifiées : ${INSTALL[*]}"
    run "${INSTALL[@]}"
  fi

  if grep -q '"build"[[:space:]]*:' package.json; then
    say "build"
    run "$PM" run build
  fi

  if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    say "redémarrage de $SERVICE"
    systemctl restart "$SERVICE"
  else
    fail "aucune unité systemd « $SERVICE » : crée-la (le panel le fait pour une nouvelle machine) ou règle SERVICE en haut du script"
  fi

else
  # --- Autre projet : on redémarre juste le service s'il existe ----------
  if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    say "redémarrage de $SERVICE"
    systemctl restart "$SERVICE"
  else
    say "rien à construire ni à redémarrer"
  fi
fi

# --- Vérification : le service tourne et l'appli répond --------------------
if systemctl cat "$SERVICE" >/dev/null 2>&1; then
  sleep 2
  if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    fail "$SERVICE ne tourne pas après le redémarrage"
  fi
fi

if [ -n "$HEALTH_URL" ]; then
  say "vérification de $HEALTH_URL"
  for _ in $(seq "$HEALTH_TIMEOUT"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" || true)
    if [ "${code:-000}" != "000" ] && [ "$code" -lt 500 ]; then
      echo "✓ l'appli répond (HTTP $code), ${NEW_SHA:0:7} en ligne"
      exit 0
    fi
    sleep 1
  done
  journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || true
  fail "l'appli ne répond pas sur $HEALTH_URL après ${HEALTH_TIMEOUT} s"
fi

echo "✓ ${NEW_SHA:0:7} déployé"
