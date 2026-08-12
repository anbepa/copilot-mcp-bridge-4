/** Carga de configuración: default.json → local.json → variables de entorno → flags CLI. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Expande "~" al home del usuario. */
function expandHome(p) {
  if (typeof p !== 'string') return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

/** Raíz del proyecto (contiene src/, config/, workspace/). */
export const ROOT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function readJson(p) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
}

function deepMerge(a, b) {
  if (b === undefined || b === null) return a;
  if (Array.isArray(b) || typeof b !== 'object') return b;
  const out = { ...(a ?? {}) };
  for (const [k, v] of Object.entries(b)) out[k] = deepMerge(a?.[k], v);
  return out;
}

export function loadConfig(overrides = {}) {
  const def = readJson(path.join(ROOT_DIR, 'config', 'default.json'));
  if (!def) throw new Error(`No se encontró ${path.join(ROOT_DIR, 'config', 'default.json')}`);
  const local = readJson(path.join(ROOT_DIR, 'config', 'local.json')) ?? {};

  let cfg = deepMerge(def, local);

  // Entorno
  const env = {};
  if (process.env.CMB_MODEL) env.driver = { ...(env.driver ?? {}), model: process.env.CMB_MODEL };
  if (process.env.CMB_URL) env.driver = { ...(env.driver ?? {}), url: process.env.CMB_URL };
  if (process.env.CMB_HEADLESS) env.driver = { ...(env.driver ?? {}), headless: process.env.CMB_HEADLESS === '1' };
  if (process.env.CMB_CDP) env.driver = { ...(env.driver ?? {}), cdpEndpoint: process.env.CMB_CDP };
  if (process.env.CMB_ROOT) env.sandbox = { roots: [process.env.CMB_ROOT] };
  cfg = deepMerge(cfg, env);

  cfg = deepMerge(cfg, overrides);

  // Resolver rutas relativas respecto a la raíz del proyecto
  const base = ROOT_DIR;
  cfg.sandbox.roots = cfg.sandbox.roots.map((r) => path.resolve(base, expandHome(r)));
  cfg.audit.dir = path.resolve(base, expandHome(cfg.audit.dir));
  // El perfil del navegador vive FUERA del proyecto (por defecto en ~/.copilot-mcp-bridge).
  // Si viviera dentro, cada vez que descomprimes una versión nueva del proyecto
  // empezarías con un perfil vacío y tendrías que volver a iniciar sesión.
  cfg.driver.userDataDir = path.resolve(base, expandHome(cfg.driver.userDataDir));
  cfg.projectDir = base;
  return cfg;
}
