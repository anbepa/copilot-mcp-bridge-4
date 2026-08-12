/**
 * Auto-bootstrap del MCP Unified Server (Python).
 *
 * Prepara TODO lo necesario para que las 83 herramientas funcionen sin que el
 * usuario instale nada a mano: crea un entorno virtual dedicado, instala las
 * dependencias (httpx para API testing y, si el navegador está habilitado,
 * Playwright + el binario de Chromium) y reescribe el `command` del servidor
 * para que apunte al Python del venv.
 *
 * Es idempotente y rápido en ejecuciones posteriores: un archivo marcador
 * (.cmb-ready) evita reinstalar si ya está todo listo con la misma
 * configuración. Cero dependencias de Node (solo librería estándar).
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { log } from '../log.mjs';

const IS_WIN = process.platform === 'win32';
const MARKER_VERSION = 1;

/** Devuelve el primer intérprete Python disponible en el sistema. */
function findSystemPython() {
  const candidates = IS_WIN ? ['py', 'python', 'python3'] : ['python3', 'python'];
  for (const cmd of candidates) {
    const r = spawnSync(cmd, ['--version'], { stdio: 'ignore' });
    if (r.status === 0) return cmd;
  }
  return null;
}

/** Ruta al ejecutable de Python dentro de un venv. */
function venvPython(venvDir) {
  return IS_WIN
    ? path.join(venvDir, 'Scripts', 'python.exe')
    : path.join(venvDir, 'bin', 'python');
}

/** Ejecuta un comando mostrando su salida en vivo. Lanza si falla. */
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (r.error) throw r.error;
  if (r.status !== 0) {
    throw new Error(`Falló: ${cmd} ${args.join(' ')} (código ${r.status})`);
  }
}

/** ¿El venv ya tiene importables los módulos indicados? (chequeo rápido) */
function canImport(py, modules) {
  const code = `import ${modules.join(', ')}`;
  const r = spawnSync(py, ['-c', code], { stdio: 'ignore' });
  return r.status === 0;
}

function isTruthy(v) {
  if (v === true) return true;
  if (typeof v !== 'string') return false;
  return ['1', 'true', 'yes', 'y', 'on', 'si', 'sí'].includes(v.trim().toLowerCase());
}

/**
 * Detecta los servidores que apuntan al stdio_server.py del unified server y
 * garantiza sus dependencias. Muta `servers[name].command` al Python del venv.
 *
 * @param {{servers:object, cwd:string}} args
 * @returns {boolean} true si se preparó al menos un servidor unificado.
 */
export function ensureUnifiedServerDeps({ servers, cwd }) {
  let touched = false;

  for (const [name, cfg] of Object.entries(servers)) {
    if (cfg.enabled === false) continue;
    const scriptArg = (cfg.args ?? []).find(
      (a) => typeof a === 'string' && a.endsWith('stdio_server.py')
    );
    if (!scriptArg) continue; // no es el unified server

    const scriptPath = path.resolve(cwd, scriptArg);
    if (!fs.existsSync(scriptPath)) {
      log.warn(`No se encontró el stdio_server.py de "${name}": ${scriptPath}`);
      continue;
    }
    const serverDir = path.dirname(scriptPath);
    const venvDir = path.join(serverDir, '.venv');
    const py = venvPython(venvDir);

    const browserEnabled = isTruthy(cfg.env?.MCP_ENABLE_BROWSER ?? true);
    const wanted = ['httpx', ...(browserEnabled ? ['playwright'] : [])];
    const markerPath = path.join(venvDir, `.cmb-ready-v${MARKER_VERSION}`);
    const markerData = JSON.stringify({ v: MARKER_VERSION, browser: browserEnabled, wanted });

    // Ruta rápida: si el marcador coincide y los módulos importan, no hacemos nada.
    if (
      fs.existsSync(py) &&
      fs.existsSync(markerPath) &&
      fs.readFileSync(markerPath, 'utf8') === markerData &&
      canImport(py, wanted)
    ) {
      cfg.command = py;
      touched = true;
      continue;
    }

    log.info(`Preparando entorno del servidor "${name}" (una sola vez)…`);

    // 1) Crear el venv si no existe.
    if (!fs.existsSync(py)) {
      const sysPy = findSystemPython();
      if (!sysPy) {
        throw new Error(
          'No se encontró Python 3. Instálalo (https://www.python.org/downloads/) y vuelve a intentarlo.'
        );
      }
      log.info('  › creando entorno virtual .venv');
      run(sysPy, ['-m', 'venv', venvDir]);
    }

    // 2) pip al día + dependencias.
    log.info('  › instalando dependencias con pip');
    run(py, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet']);
    run(py, ['-m', 'pip', 'install', '--quiet', ...wanted]);

    // 3) Binario del navegador (solo si el grupo browser_* está habilitado).
    if (browserEnabled) {
      log.info('  › descargando el navegador Chromium (~150 MB la primera vez)');
      run(py, ['-m', 'playwright', 'install', 'chromium']);
    }

    // 4) Marcador para no repetir en futuras ejecuciones.
    fs.writeFileSync(markerPath, markerData, 'utf8');
    log.ok(`Entorno de "${name}" listo`);

    cfg.command = py;
    touched = true;
  }

  return touched;
}
