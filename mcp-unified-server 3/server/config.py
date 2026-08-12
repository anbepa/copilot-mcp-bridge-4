"""
Configuración central del servidor MCP.

Todos los valores se pueden sobreescribir mediante variables de entorno
(o mediante el archivo .env que carga scripts/start.sh / start.bat).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- Red / transporte -------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("MCP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _as_int(os.getenv("MCP_PORT"), 8787))
    log_level: str = field(default_factory=lambda: os.getenv("MCP_LOG_LEVEL", "info"))

    # --- Seguridad --------------------------------------------------------
    # Si se define, todas las peticiones deben enviar:  Authorization: Bearer <token>
    auth_token: str = field(default_factory=lambda: os.getenv("MCP_AUTH_TOKEN", ""))

    # Raíz del "sandbox" de filesystem. Por defecto el directorio de trabajo.
    workspace_root: Path = field(
        default_factory=lambda: Path(
            os.getenv("MCP_WORKSPACE_ROOT", os.getcwd())
        ).expanduser().resolve()
    )
    # Si es True se permite salir de workspace_root (¡peligroso!).
    allow_outside_root: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_ALLOW_OUTSIDE_ROOT"), False)
    )
    # Habilita/inhabilita por completo el grupo de herramientas de terminal.
    enable_terminal: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_ENABLE_TERMINAL"), True)
    )
    # Lista negra de comandos (regex, separados por coma) — vacío = sin filtro.
    command_denylist: str = field(
        default_factory=lambda: os.getenv("MCP_COMMAND_DENYLIST", "")
    )

    # --- Navegador (Playwright) ------------------------------------------
    enable_browser: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_ENABLE_BROWSER"), True)
    )
    browser_engine: str = field(
        default_factory=lambda: os.getenv("MCP_BROWSER_ENGINE", "chromium").lower()
    )
    browser_headless: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_BROWSER_HEADLESS"), True)
    )
    browser_viewport_width: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_BROWSER_WIDTH"), 1280)
    )
    browser_viewport_height: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_BROWSER_HEIGHT"), 720)
    )
    browser_timeout_ms: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_BROWSER_TIMEOUT_MS"), 30_000)
    )
    browser_wait_until: str = field(
        default_factory=lambda: os.getenv("MCP_BROWSER_WAIT_UNTIL", "domcontentloaded")
    )
    browser_user_agent: str = field(
        default_factory=lambda: os.getenv("MCP_BROWSER_USER_AGENT", "")
    )
    browser_executable_path: str = field(
        default_factory=lambda: os.getenv("MCP_BROWSER_EXECUTABLE_PATH", "")
    )
    browser_output_dir: str = field(
        default_factory=lambda: os.getenv("MCP_BROWSER_OUTPUT_DIR", "")
    )
    # browser_run_code_unsafe equivale a RCE: deshabilitado por defecto.
    enable_unsafe_browser_code: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_ENABLE_UNSAFE_BROWSER_CODE"), False)
    )

    # --- API Testing & QA -------------------------------------------------
    enable_api_testing: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_ENABLE_API_TESTING"), True)
    )
    # Prefijo opcional: si se define, las URLs relativas se resuelven contra él.
    api_base_url: str = field(
        default_factory=lambda: os.getenv("MCP_API_BASE_URL", "")
    )
    api_timeout_seconds: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_TIMEOUT"), 30)
    )
    # Verificación de certificados TLS. Ponlo a false SÓLO en entornos de QA.
    api_verify_tls: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_API_VERIFY_TLS"), True)
    )
    api_follow_redirects: bool = field(
        default_factory=lambda: _as_bool(os.getenv("MCP_API_FOLLOW_REDIRECTS"), True)
    )
    api_max_redirects: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_MAX_REDIRECTS"), 10)
    )
    # Lista blanca de hosts (regex separados por coma). Vacío = cualquier host.
    api_host_allowlist: str = field(
        default_factory=lambda: os.getenv("MCP_API_HOST_ALLOWLIST", "")
    )
    # Máximo de caracteres de cuerpo que se devuelven/almacenan por respuesta.
    api_max_body_chars: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_MAX_BODY_CHARS"), 20_000)
    )
    # Nº máximo de peticiones conservadas en el historial de la sesión.
    api_max_history: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_MAX_HISTORY"), 500)
    )
    # Timeout (s) para cada script de colección evaluado en Node.
    api_script_timeout: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_SCRIPT_TIMEOUT"), 30)
    )
    # Timeout (s) total de una ejecución de colección con newman.
    api_collection_timeout: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_API_COLLECTION_TIMEOUT"), 900)
    )
    # Directorio donde generate_test_report escribe los informes.
    api_report_dir: str = field(
        default_factory=lambda: os.getenv("MCP_API_REPORT_DIR", "")
    )

    # --- Límites ----------------------------------------------------------
    command_timeout: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_COMMAND_TIMEOUT"), 120)
    )
    max_output_chars: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_MAX_OUTPUT_CHARS"), 200_000)
    )
    max_read_bytes: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_MAX_READ_BYTES"), 5_000_000)
    )
    max_search_results: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_MAX_SEARCH_RESULTS"), 500)
    )
    sse_keepalive_seconds: int = field(
        default_factory=lambda: _as_int(os.getenv("MCP_SSE_KEEPALIVE"), 15)
    )

    # --- Metadatos --------------------------------------------------------
    server_name: str = field(
        default_factory=lambda: os.getenv(
            "MCP_SERVER_NAME", "unified-fs-bash-browser-api-mcp"
        )
    )
    server_version: str = "3.0.0"
    protocol_version: str = field(
        default_factory=lambda: os.getenv("MCP_PROTOCOL_VERSION", "2024-11-05")
    )


settings = Settings()
