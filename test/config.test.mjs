/**
 * Regresión del segundo fallo real (log de Andres, 2026-08-11): tras `npm run login`
 * el siguiente `run` volvía a pedir autenticación.
 *
 * Dos causas, ambas cubiertas aquí:
 *   1. El perfil vivía DENTRO del proyecto (./.browser-profile). Al descomprimir una
 *      versión nueva ("copilot-mcp-bridge 2") aparecía un perfil vacío.
 *   2. `login` hacía process.exit(0) justo tras cerrar, truncando el volcado de
 *      cookies/tokens de Chromium.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { loadConfig, ROOT_DIR } from '../src/config.mjs';

test('el perfil por defecto vive FUERA del proyecto', () => {
  const cfg = loadConfig();
  const dir = cfg.driver.userDataDir;
  assert.ok(path.isAbsolute(dir), 'debe ser una ruta absoluta');
  assert.ok(
    !dir.startsWith(ROOT_DIR + path.sep),
    `el perfil no debe estar dentro del proyecto (${dir}); si no, se pierde al actualizar`
  );
  assert.ok(dir.startsWith(os.homedir()), 'debe colgar del home del usuario');
});

test('expande ~ correctamente', () => {
  const cfg = loadConfig({ driver: { userDataDir: '~/mi-perfil' } });
  assert.equal(cfg.driver.userDataDir, path.join(os.homedir(), 'mi-perfil'));
});

test('acepta una ruta absoluta de perfil sin tocarla', () => {
  const abs = path.join(os.tmpdir(), 'perfil-cmb');
  assert.equal(loadConfig({ driver: { userDataDir: abs } }).driver.userDataDir, abs);
});

test('una ruta relativa se ancla al proyecto, no al cwd', () => {
  // Ejecutar desde otra carpeta no debe cambiar a qué perfil apunta.
  const cfg = loadConfig({ driver: { userDataDir: './.browser-profile' } });
  assert.equal(cfg.driver.userDataDir, path.join(ROOT_DIR, '.browser-profile'));
});

test('headless es false por defecto y se puede forzar', () => {
  assert.equal(loadConfig().driver.headless, false);
  assert.equal(loadConfig({ driver: { headless: true } }).driver.headless, true);
});

test('el sandbox y la auditoría siguen anclados al proyecto', () => {
  const cfg = loadConfig();
  for (const r of cfg.sandbox.roots) assert.ok(path.isAbsolute(r));
  assert.ok(path.isAbsolute(cfg.audit.dir));
});

test('CMB_HEADLESS=1 activa headless por entorno', () => {
  const prev = process.env.CMB_HEADLESS;
  process.env.CMB_HEADLESS = '1';
  try {
    assert.equal(loadConfig().driver.headless, true);
  } finally {
    if (prev === undefined) delete process.env.CMB_HEADLESS;
    else process.env.CMB_HEADLESS = prev;
  }
});

test('los flags CLI tienen prioridad sobre el entorno', () => {
  const prev = process.env.CMB_HEADLESS;
  process.env.CMB_HEADLESS = '1';
  try {
    assert.equal(loadConfig({ driver: { headless: false } }).driver.headless, false);
  } finally {
    if (prev === undefined) delete process.env.CMB_HEADLESS;
    else process.env.CMB_HEADLESS = prev;
  }
});
