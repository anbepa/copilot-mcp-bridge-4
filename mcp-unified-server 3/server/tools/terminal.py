"""Grupo de herramientas 2: Bash / Terminal (ejecución en consola)."""
from __future__ import annotations

import asyncio
import os
import platform
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import settings
from ..core.registry import ToolError, registry
from ..core.security import assert_command_allowed, resolve_path, truncate

IS_WINDOWS = platform.system().lower().startswith("win")


# --------------------------------------------------------------------------- #
class BackgroundProcess:
    """Proceso en segundo plano con captura incremental de salida."""

    def __init__(self, process_id: str, command: str, popen: subprocess.Popen, cwd: str):
        self.process_id = process_id
        self.command = command
        self.popen = popen
        self.cwd = cwd
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self._buffer: List[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        try:
            assert self.popen.stdout is not None
            for line in self.popen.stdout:
                with self._lock:
                    self._buffer.append(line)
                    if len(self._buffer) > 5000:
                        del self._buffer[: len(self._buffer) - 5000]
        except Exception:  # noqa: BLE001  (el proceso puede cerrarse abruptamente)
            pass
        finally:
            self.popen.wait()
            self.finished_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self.popen.poll() is None

    @property
    def exit_code(self) -> Optional[int]:
        return self.popen.poll()

    def output(self) -> str:
        with self._lock:
            return "".join(self._buffer)

    def info(self) -> Dict[str, Any]:
        return {
            "processId": self.process_id,
            "pid": self.popen.pid,
            "command": self.command,
            "cwd": self.cwd,
            "status": "running" if self.running else "finished",
            "exitCode": self.exit_code,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "uptimeSeconds": round(
                ((self.finished_at or datetime.now(timezone.utc)) - self.started_at
                 ).total_seconds(),
                2,
            ),
            "outputChars": len(self.output()),
        }

    def terminate(self, force: bool = True) -> bool:
        if not self.running:
            return False
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.popen.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                pgid = os.getpgid(self.popen.pid)
                os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                self.popen.kill()
            except Exception:  # noqa: BLE001
                return False
        for _ in range(30):
            if not self.running:
                break
            time.sleep(0.1)
        return True


BACKGROUND: Dict[str, BackgroundProcess] = {}


def _popen_kwargs(cwd: str) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "shell": True,
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
        "encoding": "utf-8",
        "errors": "replace",
        "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _resolve_cwd(cwd: Optional[str]) -> str:
    if cwd:
        return str(resolve_path(cwd, must_exist=True))
    return str(settings.workspace_root)


# --------------------------------------------------------------------------- #
@registry.tool(
    name="run",
    title="Ejecutar comando",
    description=(
        "Ejecuta un comando de shell de forma síncrona (en primer plano) y espera "
        "a que termine, devolviendo stdout, stderr y el código de salida. Ideal "
        "para comandos cortos (ls, git status, npm install...). Existe un timeout."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Comando de shell a ejecutar."},
            "cwd": {
                "type": "string",
                "description": "Directorio de trabajo opcional (por defecto el workspace).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout en segundos (por defecto MCP_COMMAND_TIMEOUT=120).",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)
async def run(command: str, cwd: Optional[str] = None, timeout: int = 0) -> Dict[str, Any]:
    assert_command_allowed(command)
    workdir = _resolve_cwd(cwd)
    limit = timeout if timeout and timeout > 0 else settings.command_timeout
    started = time.time()

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise ToolError(
            f"El comando excedió el timeout de {limit}s y fue terminado. "
            "Usa 'run_background' para procesos largos."
        )

    stdout = truncate(stdout_b.decode("utf-8", errors="replace"))
    stderr = truncate(stderr_b.decode("utf-8", errors="replace"))
    return {
        "command": command,
        "cwd": workdir,
        "exitCode": proc.returncode,
        "success": proc.returncode == 0,
        "durationSeconds": round(time.time() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="run_background",
    title="Ejecutar en segundo plano",
    description=(
        "Inicia un comando de larga duración en segundo plano (servidores, "
        "watchers, builds) y devuelve inmediatamente un 'processId' para "
        "consultarlo con list_background / get_background_output o terminarlo "
        "con kill_background."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Comando de larga duración."},
            "cwd": {"type": "string", "description": "Directorio de trabajo opcional."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)
def run_background(command: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    assert_command_allowed(command)
    workdir = _resolve_cwd(cwd)
    process_id = f"proc_{uuid.uuid4().hex[:8]}"
    popen = subprocess.Popen(command, **_popen_kwargs(workdir))  # noqa: S602
    bg = BackgroundProcess(process_id, command, popen, workdir)
    BACKGROUND[process_id] = bg
    time.sleep(0.3)  # margen para capturar fallos inmediatos
    return {
        **bg.info(),
        "started": True,
        "hint": "Usa get_background_output(processId) para leer la salida acumulada.",
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="list_background",
    title="Listar procesos en segundo plano",
    description=(
        "Devuelve la lista de todos los procesos en segundo plano gestionados por "
        "el servidor, con su processId, PID, comando, estado y tiempo de vida."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def list_background() -> Dict[str, Any]:
    procs = [bg.info() for bg in BACKGROUND.values()]
    return {
        "total": len(procs),
        "running": sum(1 for p in procs if p["status"] == "running"),
        "finished": sum(1 for p in procs if p["status"] == "finished"),
        "processes": procs,
    }


# --------------------------------------------------------------------------- #
@registry.tool(
    name="kill_background",
    title="Terminar proceso en segundo plano",
    description=(
        "Finaliza de forma forzada un proceso en segundo plano (y todo su árbol de "
        "procesos hijos) usando su identificador 'processId' o su PID."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "processId": {
                "type": "string",
                "description": "Identificador devuelto por run_background (o el PID numérico).",
            }
        },
        "required": ["processId"],
        "additionalProperties": False,
    },
)
def kill_background(processId: Any) -> Dict[str, Any]:  # noqa: N803 (nombre de la spec)
    key = str(processId)
    bg = BACKGROUND.get(key)
    if bg is None:
        for candidate in BACKGROUND.values():
            if str(candidate.popen.pid) == key:
                bg = candidate
                break
    if bg is None:
        raise ToolError(
            f"No existe un proceso con processId '{key}'. "
            f"Activos: {', '.join(BACKGROUND) or 'ninguno'}"
        )
    if not bg.running:
        return {**bg.info(), "status": "already_finished", "killed": False}
    bg.terminate(force=True)
    return {**bg.info(), "status": "killed", "killed": True}


# --------------------------------------------------------------------------- #
# Herramientas adicionales de robustecimiento
# --------------------------------------------------------------------------- #
@registry.tool(
    name="get_background_output",
    title="Leer salida de un proceso en segundo plano",
    description="Devuelve la salida (stdout+stderr) acumulada por un proceso en segundo plano.",
    input_schema={
        "type": "object",
        "properties": {
            "processId": {"type": "string", "description": "Identificador del proceso."},
            "tail_lines": {
                "type": "integer",
                "description": "Devuelve solo las últimas N líneas (0 = todo).",
            },
        },
        "required": ["processId"],
        "additionalProperties": False,
    },
)
def get_background_output(processId: Any, tail_lines: int = 0) -> Dict[str, Any]:  # noqa: N803
    bg = BACKGROUND.get(str(processId))
    if bg is None:
        raise ToolError(f"No existe un proceso con processId '{processId}'.")
    output = bg.output()
    if tail_lines and tail_lines > 0:
        output = "\n".join(output.splitlines()[-tail_lines:])
    return {**bg.info(), "output": truncate(output)}


@registry.tool(
    name="get_system_info",
    title="Información del entorno",
    description=(
        "Devuelve información del servidor donde corre el MCP: sistema operativo, "
        "versión de Python, workspace configurado, política de seguridad y "
        "herramientas disponibles."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def get_system_info() -> Dict[str, Any]:
    return {
        "server": {
            "name": settings.server_name,
            "version": settings.server_version,
            "protocolVersion": settings.protocol_version,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "hostname": platform.node(),
        },
        "workspace": {
            "root": str(settings.workspace_root),
            "allowOutsideRoot": settings.allow_outside_root,
        },
        "security": {
            "authRequired": bool(settings.auth_token),
            "terminalEnabled": settings.enable_terminal,
            "commandTimeout": settings.command_timeout,
            "denylist": settings.command_denylist or None,
        },
        "tools": registry.names(),
        "backgroundProcesses": len(BACKGROUND),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
