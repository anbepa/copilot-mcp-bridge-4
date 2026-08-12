#!/usr/bin/env bash
# =============================================================================
#  MCP UNIFIED SERVER — Arranque automático (Linux / macOS / WSL)
#  1) Prepara el entorno virtual e instala dependencias
#  2) Levanta el servidor MCP local (FastAPI + SSE)
#  3) Descarga/invoca cloudflared y publica una URL pública (Quick Tunnel)
#  4) Muestra en consola las URLs listas para Copilot Studio / clientes MCP
# =============================================================================
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

VENV_DIR="${BASE_DIR}/.venv"
BIN_DIR="${BASE_DIR}/bin"
LOG_DIR="${BASE_DIR}/logs"
SERVER_LOG="${LOG_DIR}/server.log"
TUNNEL_LOG="${LOG_DIR}/cloudflared.log"
mkdir -p "$LOG_DIR" "$BIN_DIR"

# --- colores --------------------------------------------------------------
if [ -t 1 ]; then
  G="\033[0;32m"; Y="\033[1;33m"; R="\033[0;31m"; B="\033[0;36m"; BOLD="\033[1m"; N="\033[0m"
else
  G=""; Y=""; R=""; B=""; BOLD=""; N=""
fi
info()  { echo -e "${B}[INFO]${N}  $*"; }
ok()    { echo -e "${G}[ OK ]${N}  $*"; }
warn()  { echo -e "${Y}[WARN]${N}  $*"; }
err()   { echo -e "${R}[FAIL]${N}  $*"; }

# --- .env (las variables ya exportadas en el shell tienen prioridad) --------
_PRE_HOST="${MCP_HOST:-}"; _PRE_PORT="${MCP_PORT:-}"; _PRE_TUNNEL="${ENABLE_TUNNEL:-}"
if [ -f "${BASE_DIR}/.env" ]; then
  set -a; . "${BASE_DIR}/.env"; set +a
elif [ -f "${BASE_DIR}/.env.example" ]; then
  cp "${BASE_DIR}/.env.example" "${BASE_DIR}/.env"
  set -a; . "${BASE_DIR}/.env"; set +a
  info "Se creó .env a partir de .env.example"
fi

[ -n "$_PRE_HOST" ] && MCP_HOST="$_PRE_HOST"
[ -n "$_PRE_PORT" ] && MCP_PORT="$_PRE_PORT"
[ -n "$_PRE_TUNNEL" ] && ENABLE_TUNNEL="$_PRE_TUNNEL"

MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-8787}"
ENABLE_TUNNEL="${ENABLE_TUNNEL:-true}"
export MCP_HOST MCP_PORT
CLOUDFLARED_TUNNEL_TOKEN="${CLOUDFLARED_TUNNEL_TOKEN:-}"

SERVER_PID=""; TUNNEL_PID=""
cleanup() {
  echo ""
  info "Cerrando servicios..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null && ok "cloudflared detenido (PID $TUNNEL_PID)"
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && ok "servidor MCP detenido (PID $SERVER_PID)"
  sleep 1
  exit 0
}
trap cleanup INT TERM

echo ""
echo -e "${BOLD}=============================================================${N}"
echo -e "${BOLD}  MCP UNIFIED SERVER · Filesystem+Terminal+Browser+API Testing · SSE${N}"
echo -e "${BOLD}=============================================================${N}"

# =============================================================================
# 1) Python + dependencias
# =============================================================================
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
[ -z "$PY" ] && { err "Se requiere Python 3.9+ en el PATH."; exit 1; }
ok "Python detectado: $($PY --version 2>&1)"

VENV_PY=""
if [ "${SKIP_VENV:-false}" != "true" ]; then
  if [ ! -d "$VENV_DIR" ]; then
    info "Creando entorno virtual (.venv)..."
    "$PY" -m venv "$VENV_DIR" >/dev/null 2>&1 || warn "No se pudo crear el venv; se usará el Python del sistema."
  fi
  for cand in "${VENV_DIR}/bin/python" "${VENV_DIR}/Scripts/python.exe"; do
    [ -x "$cand" ] && VENV_PY="$cand" && break
  done
fi
[ -z "$VENV_PY" ] && VENV_PY="$PY"

if ! "$VENV_PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  info "Instalando dependencias (fastapi, uvicorn)..."
  "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
  if ! "$VENV_PY" -m pip install -r "${BASE_DIR}/requirements.txt"; then
    # Último intento: Python del sistema
    if "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
      warn "Usando el Python del sistema (ya tiene las dependencias)."
      VENV_PY="$PY"
    else
      err "Fallo instalando dependencias. Ejecuta manualmente:"
      err "   $PY -m pip install -r requirements.txt"
      exit 1
    fi
  fi
fi
ok "Dependencias listas ($("$VENV_PY" --version 2>&1))"

# ---- Navegador (opcional): ENABLE_BROWSER=true instala Playwright ----------
if [ "${ENABLE_BROWSER:-false}" = "true" ]; then
  if "$VENV_PY" -c "import playwright" >/dev/null 2>&1; then
    ok "Playwright ya está instalado"
  else
    info "Instalando Playwright (grupo de tools browser_*)..."
    "$VENV_PY" -m pip install -r "${BASE_DIR}/requirements-browser.txt" \
      || warn "No se pudo instalar Playwright; las tools browser_* devolverán un error accionable."
  fi
  BROWSER_ENGINE="${MCP_BROWSER_ENGINE:-chromium}"
  if "$VENV_PY" -c "import playwright" >/dev/null 2>&1; then
    info "Descargando el navegador '${BROWSER_ENGINE}' (puede tardar la primera vez)..."
    "$VENV_PY" -m playwright install "$BROWSER_ENGINE" \
      || warn "No se pudo descargar el navegador. Usa la tool 'browser_install' más tarde."
    "$VENV_PY" -m playwright install-deps "$BROWSER_ENGINE" >/dev/null 2>&1 || true
    ok "Navegador '${BROWSER_ENGINE}' listo"
  fi
else
  if "$VENV_PY" -c "import playwright" >/dev/null 2>&1; then
    ok "Playwright detectado: las tools browser_* están operativas"
  else
    info "Playwright no instalado — las tools browser_* se publican pero pedirán instalación."
    info "Para habilitarlas:  ENABLE_BROWSER=true ./start.sh"
  fi
fi

# =============================================================================
# 2) Validación de sintaxis + puerto libre
# =============================================================================
"$VENV_PY" -m compileall -q "${BASE_DIR}/server" "${BASE_DIR}/main.py" >/dev/null 2>&1 \
  && ok "Validación de sintaxis correcta" || warn "Advertencias en la compilación"

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$MCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "El puerto ${MCP_PORT} está ocupado; buscando uno libre..."
  MCP_PORT=$("$VENV_PY" - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
)
  export MCP_PORT
  ok "Nuevo puerto asignado: ${MCP_PORT}"
fi

# =============================================================================
# 3) Arranque del servidor MCP
# =============================================================================
info "Levantando servidor MCP en http://${MCP_HOST}:${MCP_PORT} ..."
MCP_HOST="$MCP_HOST" MCP_PORT="$MCP_PORT" nohup "$VENV_PY" "${BASE_DIR}/main.py" \
  > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

HEALTH_URL="http://127.0.0.1:${MCP_PORT}/health"
READY="false"
for _ in $(seq 1 40); do
  if "$VENV_PY" - "$HEALTH_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=2).read()
PY
  then READY="true"; break; fi
  sleep 0.5
done

if [ "$READY" != "true" ]; then
  err "El servidor no respondió en ${HEALTH_URL}. Últimas líneas del log:"
  tail -n 30 "$SERVER_LOG"
  cleanup
fi
ok "Servidor MCP activo (PID ${SERVER_PID}) — log: ${SERVER_LOG}"

# =============================================================================
# 4) Cloudflare Tunnel
# =============================================================================
PUBLIC_URL=""
if [ "$ENABLE_TUNNEL" = "true" ]; then
  CF_BIN="$(command -v cloudflared || true)"
  if [ -z "$CF_BIN" ] && [ -x "${BIN_DIR}/cloudflared" ]; then
    CF_BIN="${BIN_DIR}/cloudflared"
  fi

  if [ -z "$CF_BIN" ]; then
    info "cloudflared no encontrado. Descargando binario oficial..."
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64|amd64) ARCH="amd64" ;;
      aarch64|arm64) ARCH="arm64" ;;
      armv7l) ARCH="arm" ;;
    esac
    if [ "$OS" = "darwin" ]; then
      URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${ARCH}.tgz"
      if curl -fsSL "$URL" -o "${BIN_DIR}/cf.tgz"; then
        tar -xzf "${BIN_DIR}/cf.tgz" -C "$BIN_DIR" && rm -f "${BIN_DIR}/cf.tgz"
        chmod +x "${BIN_DIR}/cloudflared" 2>/dev/null
        CF_BIN="${BIN_DIR}/cloudflared"
      fi
    else
      URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
      if curl -fsSL "$URL" -o "${BIN_DIR}/cloudflared"; then
        chmod +x "${BIN_DIR}/cloudflared"
        CF_BIN="${BIN_DIR}/cloudflared"
      fi
    fi
  fi

  if [ -z "$CF_BIN" ] || [ ! -x "$CF_BIN" ]; then
    warn "No se pudo obtener cloudflared (¿sin conexión?). El servidor sigue disponible en local."
  else
    ok "cloudflared: $CF_BIN"
    : > "$TUNNEL_LOG"
    if [ -n "$CLOUDFLARED_TUNNEL_TOKEN" ]; then
      info "Iniciando túnel con token configurado (dominio propio)..."
      nohup "$CF_BIN" tunnel --no-autoupdate run --token "$CLOUDFLARED_TUNNEL_TOKEN" \
        > "$TUNNEL_LOG" 2>&1 &
      TUNNEL_PID=$!
      PUBLIC_URL="${CLOUDFLARED_HOSTNAME:-<tu-dominio-configurado-en-cloudflare>}"
      sleep 5
    else
      info "Iniciando Cloudflare Quick Tunnel (URL efímera trycloudflare.com)..."
      nohup "$CF_BIN" tunnel --no-autoupdate --url "http://127.0.0.1:${MCP_PORT}" \
        > "$TUNNEL_LOG" 2>&1 &
      TUNNEL_PID=$!
      for _ in $(seq 1 60); do
        PUBLIC_URL="$(grep -Eo 'https://[a-zA-Z0-9._-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"
        [ -n "$PUBLIC_URL" ] && break
        sleep 1
      done
    fi

    if [ -z "$PUBLIC_URL" ]; then
      warn "No se pudo obtener la URL pública. Revisa ${TUNNEL_LOG}"
    fi
  fi
else
  info "ENABLE_TUNNEL=false → se omite el túnel de Cloudflare."
fi

# =============================================================================
# 5) Resumen
# =============================================================================
echo ""
echo -e "${BOLD}${G}=============================================================${N}"
echo -e "${BOLD}${G}   ✅  SERVIDOR MCP EN LÍNEA${N}"
echo -e "${BOLD}${G}=============================================================${N}"
echo -e "  ${BOLD}Local${N}"
echo -e "    SSE            : http://127.0.0.1:${MCP_PORT}/sse"
echo -e "    Streamable HTTP: http://127.0.0.1:${MCP_PORT}/mcp"
echo -e "    Health         : http://127.0.0.1:${MCP_PORT}/health"
if [ -n "$PUBLIC_URL" ]; then
echo ""
echo -e "  ${BOLD}${Y}URL PÚBLICA (Cloudflare Tunnel)${N}"
echo -e "    Base           : ${BOLD}${PUBLIC_URL}${N}"
echo -e "    ${BOLD}${G}MCP SSE  →  ${PUBLIC_URL}/sse${N}"
echo -e "    MCP Stream →  ${PUBLIC_URL}/mcp"
echo ""
echo -e "  ${BOLD}Copilot Studio${N}: usa ${PUBLIC_URL}/sse como 'Server URL' del conector MCP."
fi
echo ""
echo -e "  Logs: ${SERVER_LOG}"
[ -n "$TUNNEL_PID" ] && echo -e "        ${TUNNEL_LOG}"
echo -e "  ${Y}Pulsa Ctrl+C para detener todo.${N}"
echo -e "${BOLD}${G}=============================================================${N}"
echo ""

# Mantener vivo el script mientras el servidor corra
while kill -0 "$SERVER_PID" 2>/dev/null; do sleep 2; done
warn "El proceso del servidor terminó."
cleanup
