#!/usr/bin/env bash
# CCP: set up and start everything on this machine, in one go.
#
#   ./run.sh            first time (no .env yet): asks a few questions, installs and starts everything
#   ./run.sh            already set up (a .env exists, for example after a git pull): updates and starts
#                       everything WITHOUT asking anything (same as ./run.sh update)
#   ./run-laptop.sh     the same update, for the machine that already runs CCP (keeps .env and data)
#   ./run.sh developer  on a developer's machine: only the capture (asks for the server's address and token)
#   ./run.sh status     only show what is running
#   ./run.sh check      the latest captured sessions: which app, report.md, changes, decisions
#   ./run.sh stop       stop the CCP server and the containers (data is kept)
#
# What it does, in order:
#   1. checks the tools (python 3.12+, git, docker with compose, claude)
#   2. makes the Python environment (.venv) and installs CCP
#   3. writes .env with strong random passwords (only if .env does not exist yet)
#   4. starts PostgreSQL, MinIO and Metabase in Docker and waits for them
#   5. applies the database migrations and creates the read-only roles
#   6. starts the CCP server (API, indexer, catalog, decisions, Explorer website) on this machine,
#      so that the decisions can use your logged-in `claude`; as a systemd user service when possible
#   7. sets up the Metabase dashboard and the first Explorer admin user
#   8. sets up capture in Claude Code: hooks, thinking summaries, the GitHub capture repo, an SSH key,
#      the background watcher and (if you say yes) the global Git hooks
#   9. prints where everything is
#
# Questions are asked only for what must come from a person (the GitHub capture repo, the admin
# e-mail, ...). Answers are kept in .env and in Claude Code's settings, so a second run asks nothing.

set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
DOCKER=(docker)
COMPOSE=()
UPDATE=0
[[ "${1:-}" == "update" || ( -f .env && -z "${1:-}" ) ]] && UPDATE=1
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
[[ -f "$VENV/Scripts/python.exe" ]] && PY="$VENV/Scripts/python.exe"
LOG_DIR="$HOME/.ccp"
SERVER_LOG="$LOG_DIR/server.log"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✘ %s\033[0m\n' "$*"; exit 1; }
ask() { # ask "question" "default" -> answer (an update never asks: it uses the default)
  local answer
  if [[ "$UPDATE" == "1" || ! -r /dev/tty ]]; then printf '%s' "$2"; return; fi
  read -r -p "  $1 [${2}]: " answer </dev/tty || true
  printf '%s' "${answer:-$2}"
}
yes_no() { # yes_no "question" "y|n"
  local answer
  answer="$(ask "$1 (y/n)" "$2")"
  [[ "$answer" =~ ^[Yy] ]]
}
ask_password() { # ask_password "label" -> password typed twice (hidden), at least 10 characters
  local first second
  while true; do
    read -r -s -p "  $1 (hidden, 10+ characters): " first </dev/tty; echo >&2
    read -r -s -p "  Type it again: " second </dev/tty; echo >&2
    [[ "$first" == "$second" && ${#first} -ge 10 ]] && { printf '%s' "$first"; return; }
    echo "  They differ or are shorter than 10 characters; try again." >&2
  done
}
envget() { # value of a key in .env (Windows line endings tolerated)
  tr -d '\r' < .env | grep -E "^$1=" | tail -1 | cut -d= -f2-
}
load_env() { set -a; # shellcheck disable=SC1090
  . <(tr -d '\r' < .env); set +a; }
port_open() { "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1]))) == 0 else 1)" "$1" 2>/dev/null; }
pick_docker() { # docker as this user, or with sudo when the user is not allowed to use it (no docker group)
  if docker info >/dev/null 2>&1; then DOCKER=(docker); return 0; fi
  if command -v sudo >/dev/null && sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker); return 0; fi
  if command -v sudo >/dev/null && [[ -t 0 && "$UPDATE" != "1" ]]; then
    echo "  Docker needs administrator rights on this machine; sudo may ask for your password."
    if sudo docker info >/dev/null 2>&1; then DOCKER=(sudo docker); return 0; fi
  fi
  return 1
}
pip_install() { # pip_install <args>: works in a venv without pip too (made by uv, or by an old python)
  if "$PY" -m pip --version >/dev/null 2>&1; then "$PY" -m pip install -q "$@"
  elif "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then "$PY" -m pip install -q "$@"
  elif command -v uv >/dev/null; then uv pip install -q --python "$PY" "$@"
  else fail "pip is missing in .venv and neither ensurepip nor uv can add it: delete .venv and run again"
  fi
}
compose() { "${DOCKER[@]}" compose --env-file .env -f infra/docker-compose.yml "$@"; }
health() { curl -s -m 3 "http://127.0.0.1:${CCP_API_PORT:-8000}/health" 2>/dev/null | grep -q '"status":"ok"'; }

# ---------------------------------------------------------------------------------- small commands
if [[ "${1:-}" == "check" ]]; then
  [[ -f .env ]] || fail "no .env yet: run ./run.sh first"
  load_env
  health && ok "CCP server is answering" || warn "CCP server is NOT answering: run ./run.sh"
  "$PY" - <<'PYCHECK'
from ccp.db.connection import connect
from ccp.settings import load_database_settings, load_storage_settings
from ccp.storage import connect_store
c = connect(load_database_settings()); k = c.cursor()
store = connect_store(load_storage_settings())
keys = set(store.list_keys("catalog/accounts/"))
k.execute("""
  SELECT s.session_id, COALESCE(e.surface, '?'), COALESCE(e.entrypoint, '?'), s.last_activity_at,
         (SELECT count(*) FROM shadow_commit x WHERE x.session_id = s.session_id),
         (SELECT count(*) FROM file_change f WHERE f.session_id = s.session_id),
         (SELECT count(*) FROM decision d WHERE d.session_id = s.session_id)
    FROM session s LEFT JOIN session_env e USING (session_id)
   WHERE NOT s.is_empty ORDER BY s.last_activity_at DESC NULLS LAST LIMIT 8""")
print(f"  {'session':10} {'app':10} {'entrypoint':16} {'last activity (UTC)':20} {'report':7} {'commits':>7} {'changes':>7} {'decisions':>9}")
for sid, surface, entry, last, commits, changes, decisions in k.fetchall():
    report = any(key.endswith(f"__{sid[:8]}/report.md") for key in keys)
    when = last.strftime("%Y-%m-%d %H:%M") if last else "-"
    print(f"  {sid[:8]:10} {surface:10} {entry:16} {when:20} {'yes' if report else 'NO':7} {commits:>7} {changes:>7} {decisions:>9}")
PYCHECK
  exit 0
fi

if [[ "${1:-}" == "status" || "${1:-}" == "stop" ]]; then
  [[ -f .env ]] || fail "no .env yet: run ./run.sh first"
  load_env
  if [[ "$1" == "stop" ]]; then
    systemctl --user stop ccp-server 2>/dev/null || pkill -f "ccp.server" 2>/dev/null || true
    pick_docker && compose --profile dashboards stop >/dev/null 2>&1 || true
    ok "stopped (the data is kept in the Docker volumes)"; exit 0
  fi
  health && ok "CCP server: http://127.0.0.1:${CCP_API_PORT:-8000}/health" || warn "CCP server is not answering"
  pick_docker && compose --profile dashboards ps 2>/dev/null || true
  exit 0
fi

setup_capture() { # setup_capture <server address> <ingest token>: Claude Code on this machine
  local API_URL="$1" TOKEN="$2"
  SETTINGS="$HOME/.claude/settings.json"
  "$PY" -m ccp.capture install --api-url "$API_URL" --token "$TOKEN" >/dev/null
  ok "8 hooks and thinking summaries in $SETTINGS"
  CURRENT_REMOTE="$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('env',{}).get('CCP_SNAPSHOT_REMOTE',''))" "$SETTINGS")"
  if [[ -z "$CURRENT_REMOTE" ]]; then
    echo "  The capture repository keeps the code at every step (ask your senior for its address)."
    CURRENT_REMOTE="$(ask "GitHub capture repo (SSH form, e.g. git@github.com:org/repo.git; empty = keep local)" "")"
  fi
  "$PY" - "$SETTINGS" "$CURRENT_REMOTE" <<'EOF'
import json, sys
path, remote = sys.argv[1], sys.argv[2]
data = json.load(open(path))
env = data.setdefault("env", {})
env["CCP_SNAPSHOTS"] = "true"
if remote:
    env["CCP_SNAPSHOT_REMOTE"] = remote
json.dump(data, open(path, "w"), indent=2)
open(path, "a").write("\n")
EOF
  if [[ "$CURRENT_REMOTE" == git@* ]]; then
    [[ -f "$HOME/.ssh/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C "ccp-capture@$(hostname)" -f "$HOME/.ssh/id_ed25519"
    if GIT_TERMINAL_PROMPT=0 timeout 20 git ls-remote "$CURRENT_REMOTE" >/dev/null 2>&1; then
      ok "GitHub accepts this machine's key: captures will be pushed to $CURRENT_REMOTE"
    else
      echo
      echo "  Add this key to the capture repo on GitHub: Settings -> Deploy keys -> Add deploy key,"
      echo "  tick 'Allow write access' (or add it to your account under Settings -> SSH keys):"
      echo
      echo "    $(cat "$HOME/.ssh/id_ed25519.pub")"
      echo
      [[ "$UPDATE" == "1" ]] || read -r -p "  Press Enter when it is added... " _ </dev/tty || true
      GIT_TERMINAL_PROMPT=0 timeout 20 git ls-remote "$CURRENT_REMOTE" >/dev/null 2>&1 \
        && ok "GitHub accepts the key" \
        || warn "GitHub still refuses the key: captures wait locally and are pushed once it works"
    fi
  elif [[ -n "$CURRENT_REMOTE" ]]; then
    warn "an https address needs a stored token for pushes without a prompt; the SSH form is easier"
  else
    warn "no capture repo: snapshots stay in each local repository"
  fi
  "$PY" -m ccp.capture watch-service install >/dev/null && ok "background watcher starts at login" \
    || warn "watcher service not installed: run 'python -m ccp.capture watch' yourself"
  if [[ "$UPDATE" != "1" ]] && yes_no "Install the global Git hooks (a note on each commit, a backup before each push)?" "y"; then
    "$PY" -m ccp.capture git-hooks install | sed 's/^/  /'
  fi

}

# ---------------------------------------------------------------------------------- developer mode
if [[ "${1:-}" == "developer" ]]; then
  bold "Developer machine: capture only"
  command -v git >/dev/null || fail "git is missing"
  command -v claude >/dev/null || fail "install Claude Code first (the claude command)"
  PYTHON="$(command -v python3.13 || command -v python3.12 || command -v python3 || true)"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || fail "python 3.12+ is needed"
  [[ -x "$PY" ]] || "$PYTHON" -m venv "$VENV"
  pip_install -e .
  ok "capture client installed"
  echo "  Ask the person who runs the CCP server for its address and the ingest token."
  SERVER="$(ask "CCP server address" "http://127.0.0.1:8000")"
  read -r -s -p "  Ingest token (hidden): " TOKEN_IN </dev/tty; echo
  curl -s -m 5 "$SERVER/health" | grep -q '"status":"ok"' && ok "the server answers" || warn "the server does not answer yet at $SERVER"
  setup_capture "$SERVER" "$TOKEN_IN"
  bold "Done"
  echo "  Start a NEW Claude Code session: it is captured and appears on the server after the first reply."
  exit 0
fi

# ---------------------------------------------------------------------------------- 1. tools
bold "1/9 Checking the tools"
command -v git >/dev/null || fail "git is missing: install it first"
command -v curl >/dev/null || fail "curl is missing: install it first"
PYTHON="$(command -v python3.13 || command -v python3.12 || command -v python3 || true)"
[[ -n "$PYTHON" ]] || fail "python 3.12 or newer is missing"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || fail "python 3.12 or newer is needed (found $("$PYTHON" --version))"
ok "$("$PYTHON" --version), $(git --version)"
command -v docker >/dev/null || fail "docker is missing: install Docker (with the compose plugin) first"
docker compose version >/dev/null 2>&1 || sudo -n docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is missing"
DOCKER_OK=1
if pick_docker; then
  ok "docker usable ($([[ "${DOCKER[0]}" == sudo ]] && echo "with sudo" || echo "as $USER"))"
else
  DOCKER_OK=0
  if [[ "$UPDATE" == "1" ]]; then
    warn "docker is not usable without a password here: the containers are left as they are (checked below)"
  else
    fail "docker is installed but $USER may not use it (add $USER to the docker group and log in again, or allow sudo)"
  fi
fi
if command -v claude >/dev/null; then
  ok "claude $(claude --version 2>/dev/null | head -1)"
else
  warn "the claude command is missing: install Claude Code first (capture and decisions need it)"
fi

# ---------------------------------------------------------------------------------- 2. python
bold "2/9 Python environment"
[[ -x "$PY" ]] || "$PYTHON" -m venv "$VENV"
pip_install -e ".[server]"
ok "CCP installed in .venv"

# ---------------------------------------------------------------------------------- 3. .env
bold "3/9 Settings (.env)"
if [[ ! -f .env ]]; then
  rand() { "$PY" -c "import secrets; print(secrets.token_urlsafe($1))"; }
  echo "  One admin account is used for the Metabase dashboards and the Explorer website (full access)."
  MB_EMAIL="$(ask "Admin e-mail" "admin@example.com")"
  ADMIN_PASSWORD="$(ask_password "Admin password")"
  if yes_no "Will other developers' machines send their sessions to this server?" "n"; then
    API_HOST=0.0.0.0; BIND=0.0.0.0
  else
    API_HOST=127.0.0.1; BIND=127.0.0.1
  fi
  cat > .env <<EOF
# Written by run.sh on $(date -u +%Y-%m-%d). Local values; keep this file private (it is git-ignored).
POSTGRES_USER=ccp
POSTGRES_PASSWORD=$(rand 18)
POSTGRES_DB=ccp
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
MINIO_ROOT_USER=ccp-admin
MINIO_ROOT_PASSWORD=$(rand 18)
MINIO_API_PORT=9000
MINIO_CONSOLE_PORT=9001
MINIO_BUCKET=ccp-raw
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_SECURE=false
CCP_API_HOST=$API_HOST
CCP_SERVER_BIND=$BIND
CCP_API_PORT=8000
CCP_INGEST_TOKEN=$(rand 24)
CCP_SESSION_IDLE_MINUTES=30
CCP_READER_PASSWORD=$(rand 18)
CCP_AUDITOR_PASSWORD=$(rand 18)
CCP_METABASE_ADMIN_EMAIL=$MB_EMAIL
CCP_METABASE_ADMIN_PASSWORD=$ADMIN_PASSWORD
METABASE_PORT=3000
CCP_CATALOG_ENABLED=true
CCP_DECISIONS_ENABLED=true
CCP_EXPLORER_ENABLED=true
CCP_EXPLORER_SECRET=$(rand 32)
EOF
  # MinIO's access key is its root user unless set otherwise.
  printf 'MINIO_ACCESS_KEY=ccp-admin\nMINIO_SECRET_KEY=%s\n' "$(envget MINIO_ROOT_PASSWORD)" >> .env
  chmod 600 .env
  ok ".env written with new random passwords"
else
  ok ".env exists: kept as it is"
  # Ensure newer server plug-in settings exist in .env
  "$PY" - <<'EOF'
import secrets
from pathlib import Path
p = Path(".env")
if p.exists():
    text = p.read_text(encoding="utf-8")
    changed = False
    if "CCP_DECISIONS_ENABLED=" not in text:
        text += "\nCCP_DECISIONS_ENABLED=true\n"
        changed = True
    elif "CCP_DECISIONS_ENABLED=false" in text:
        text = text.replace("CCP_DECISIONS_ENABLED=false", "CCP_DECISIONS_ENABLED=true")
        changed = True
    if "CCP_CATALOG_ENABLED=" not in text:
        text += "\nCCP_CATALOG_ENABLED=true\n"
        changed = True
    if "CCP_EXPLORER_ENABLED=" not in text:
        text += "\nCCP_EXPLORER_ENABLED=true\n"
        changed = True
    if "CCP_EXPLORER_SECRET=" not in text:
        text += f"\nCCP_EXPLORER_SECRET={secrets.token_urlsafe(32)}\n"
        changed = True
    if changed:
        p.write_text(text, encoding="utf-8")
EOF
fi
load_env

# ---------------------------------------------------------------------------------- 4. containers
bold "4/9 Database, object store and dashboards (Docker)"
if [[ "$DOCKER_OK" == "1" ]]; then
  # The CCP server runs on this machine (step 6), not in Docker: stop a container version if one runs.
  compose stop ccp-server >/dev/null 2>&1 || true
  compose --profile dashboards up -d postgres minio metabase >/dev/null
  for _ in $(seq 1 60); do port_open "${POSTGRES_PORT:-5432}" && break; sleep 2; done
else
  port_open "${POSTGRES_PORT:-5432}" || fail "PostgreSQL is not running on port ${POSTGRES_PORT:-5432} and docker is not usable: start the containers (sudo docker compose --env-file .env -f infra/docker-compose.yml --profile dashboards up -d postgres minio metabase)"
  port_open "${MINIO_API_PORT:-9000}" || fail "MinIO is not running on port ${MINIO_API_PORT:-9000}"
fi
port_open "${POSTGRES_PORT:-5432}" || fail "PostgreSQL did not start"
ok "PostgreSQL ready"
for _ in $(seq 1 30); do curl -s -m 2 "http://127.0.0.1:${MINIO_API_PORT}/minio/health/live" >/dev/null && break; sleep 2; done
ok "MinIO ready (console http://127.0.0.1:${MINIO_CONSOLE_PORT}, user ${MINIO_ROOT_USER})"

# ---------------------------------------------------------------------------------- 5. database
bold "5/9 Database migrations and roles"
"$PY" -m ccp.db migrate
"$PY" -m ccp.db create-reader >/dev/null && ok "read-only role for dashboards"
"$PY" -m ccp.db create-auditor >/dev/null 2>&1 && ok "read-only role for auditors" || warn "auditor role skipped"

# ---------------------------------------------------------------------------------- 6. server
bold "6/9 CCP server (API, indexer, catalog, decisions, Explorer)"
mkdir -p "$LOG_DIR"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT="$HOME/.config/systemd/user/ccp-server.service"
  mkdir -p "$(dirname "$UNIT")"
  CLAUDE_DIR="$(dirname "$(command -v claude 2>/dev/null || echo /usr/bin/claude)")"
  cat > "$UNIT" <<EOF
[Unit]
Description=CCP server (ingestion API, indexer, catalog, decisions, Explorer)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$PY -m ccp.server --migrate
Environment=PATH=$CLAUDE_DIR:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.claude/local:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  pkill -f "ccp.server --migrate" 2>/dev/null || true   # an older copy started by hand
  systemctl --user daemon-reload
  systemctl --user enable ccp-server >/dev/null 2>&1
  systemctl --user restart ccp-server
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  ok "running as a systemd user service (starts at boot; logs: journalctl --user -u ccp-server -f)"
else
  pkill -f "ccp.server --migrate" 2>/dev/null || true
  nohup "$PY" -m ccp.server --migrate >> "$SERVER_LOG" 2>&1 &
  ok "running in the background (log: $SERVER_LOG); run ./run.sh again after a reboot"
fi
for _ in $(seq 1 30); do health && break; sleep 1; done
health || fail "the CCP server did not answer: see the log"
ok "server healthy: http://127.0.0.1:${CCP_API_PORT}/health"

# ---------------------------------------------------------------------------------- 7. dashboards, admin
bold "7/9 Dashboard and Explorer admin"
for _ in $(seq 1 90); do curl -s -m 2 "http://127.0.0.1:${METABASE_PORT}/api/health" | grep -q ok && break; sleep 2; done
if "$PY" -m ccp.dashboards setup; then ok "Metabase dashboard ready"; else warn "dashboard setup failed: run 'python -m ccp.dashboards setup' later"; fi
HAS_ADMIN="$("$PY" - <<'EOF'
from ccp.db.connection import connect
from ccp.settings import load_database_settings
c = connect(load_database_settings()); k = c.cursor()
k.execute("SELECT count(*) FROM ui_user WHERE role = 'admin' AND NOT disabled"); print(k.fetchone()[0]); c.close()
EOF
)"
if [[ "$HAS_ADMIN" == "0" ]]; then
  # The same admin as Metabase, with the role admin: every page, every chat, users, labels, access log.
  CCP_EXPLORER_PASSWORD="$CCP_METABASE_ADMIN_PASSWORD" "$PY" -m ccp.explorer create-user \
    "$CCP_METABASE_ADMIN_EMAIL" --role admin --name "Admin" >/dev/null
  ok "Explorer admin $CCP_METABASE_ADMIN_EMAIL created (role admin: full access)"
else
  ok "an Explorer admin exists"
fi

# ---------------------------------------------------------------------------------- 8. capture
bold "8/9 Capture on this machine"
setup_capture "http://127.0.0.1:${CCP_API_PORT}" "$CCP_INGEST_TOKEN"

# ---------------------------------------------------------------------------------- 9. summary
bold "9/9 Ready"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF
  What                 Address                                   User / password
  -------------------  ----------------------------------------  -----------------------------------------
  Explorer website     http://127.0.0.1:${CCP_API_PORT}/explorer     ${CCP_METABASE_ADMIN_EMAIL} / ${CCP_METABASE_ADMIN_PASSWORD}  (admin)
  Metabase dashboards  http://127.0.0.1:${METABASE_PORT}              ${CCP_METABASE_ADMIN_EMAIL} / ${CCP_METABASE_ADMIN_PASSWORD}  (admin)
  MinIO console        http://127.0.0.1:${MINIO_CONSOLE_PORT}              ${MINIO_ROOT_USER} / ${MINIO_ROOT_PASSWORD}  (root, full access)
  PostgreSQL           127.0.0.1:${POSTGRES_PORT}, database ${POSTGRES_DB}      ${POSTGRES_USER} / ${POSTGRES_PASSWORD}  (owner, full access)
  CCP server           http://127.0.0.1:${CCP_API_PORT}/health
  Capture repo         ${CURRENT_REMOTE:-local only}

  For developers' machines (./run.sh developer):
    server address     http://${LAN_IP:-this-machine}:${CCP_API_PORT}  (reachable only if you chose to share it)
    ingest token       ${CCP_INGEST_TOKEN}

  All of these are in .env too. Keep them private.

  Start a NEW Claude Code session (open chats keep their old settings). After the first reply the
  session appears in MinIO under catalog/accounts/.../sessions/<date>__<title>__<id>/ with report.md,
  within a few seconds; decisions follow about a minute after the session goes quiet or ends.

  ./run.sh status    what is running        ./run.sh stop    stop it all (data is kept)
  ./run.sh check     the latest captured sessions and whether each has its report
EOF
if [[ "$UPDATE" == "1" ]]; then
  bold "Check"
  "$0" check || true
fi
