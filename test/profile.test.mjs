/**
 * La migración es conservadora a propósito: ante la duda no hace nada.
 * Perder una sesión (y obligar a un MFA corporativo otra vez) es más caro que
 * dejar una carpeta duplicada.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { migrateLegacyProfile, looksLikeProfile } from '../src/util/profile.mjs';

let tmp;
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cmb-prof-'));
});
afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

/** Crea algo que Chromium reconocería como perfil. */
function makeProfile(dir, marker = 'Local State') {
  fs.mkdirSync(path.join(dir, 'Default'), { recursive: true });
  fs.writeFileSync(path.join(dir, marker), '{"os_crypt":{}}');
  fs.writeFileSync(path.join(dir, 'Default', 'Cookies'), 'token-falso');
  return dir;
}

test('migra el perfil antiguo del proyecto al home', () => {
  const projectDir = path.join(tmp, 'proyecto');
  makeProfile(path.join(projectDir, '.browser-profile'));
  const userDataDir = path.join(tmp, 'home', '.copilot-mcp-bridge', 'browser-profile');

  const r = migrateLegacyProfile({ projectDir, userDataDir });

  assert.equal(r.migrated, true);
  assert.ok(fs.existsSync(path.join(userDataDir, 'Local State')));
  assert.equal(fs.readFileSync(path.join(userDataDir, 'Default', 'Cookies'), 'utf8'), 'token-falso');
});

test('NO pisa un perfil nuevo que ya existe', () => {
  const projectDir = path.join(tmp, 'proyecto');
  makeProfile(path.join(projectDir, '.browser-profile'));
  const userDataDir = path.join(tmp, 'nuevo');
  makeProfile(userDataDir);
  fs.writeFileSync(path.join(userDataDir, 'Default', 'Cookies'), 'sesion-actual');

  const r = migrateLegacyProfile({ projectDir, userDataDir });

  assert.equal(r.migrated, false);
  assert.equal(r.reason, 'target-exists');
  assert.equal(
    fs.readFileSync(path.join(userDataDir, 'Default', 'Cookies'), 'utf8'),
    'sesion-actual',
    'la sesión activa no debe sobrescribirse jamás'
  );
});

test('nunca borra el perfil antiguo', () => {
  const projectDir = path.join(tmp, 'proyecto');
  const legacy = makeProfile(path.join(projectDir, '.browser-profile'));
  migrateLegacyProfile({ projectDir, userDataDir: path.join(tmp, 'destino') });
  assert.ok(fs.existsSync(legacy), 'el original se conserva por si algo va mal');
});

test('no hace nada si no hay perfil antiguo', () => {
  const r = migrateLegacyProfile({
    projectDir: path.join(tmp, 'vacio'),
    userDataDir: path.join(tmp, 'destino')
  });
  assert.equal(r.migrated, false);
  assert.equal(r.reason, 'no-legacy');
  assert.equal(fs.existsSync(path.join(tmp, 'destino')), false);
});

test('ignora una carpeta .browser-profile vacía', () => {
  const projectDir = path.join(tmp, 'proyecto');
  fs.mkdirSync(path.join(projectDir, '.browser-profile'), { recursive: true });
  const r = migrateLegacyProfile({ projectDir, userDataDir: path.join(tmp, 'destino') });
  assert.equal(r.migrated, false, 'una carpeta vacía no es una sesión que valga la pena migrar');
});

test('no se migra a sí mismo', () => {
  const projectDir = path.join(tmp, 'proyecto');
  const legacy = makeProfile(path.join(projectDir, '.browser-profile'));
  const r = migrateLegacyProfile({ projectDir, userDataDir: legacy });
  assert.equal(r.migrated, false);
  assert.equal(r.reason, 'same');
});

test('es idempotente: la segunda vez no hace nada', () => {
  const projectDir = path.join(tmp, 'proyecto');
  makeProfile(path.join(projectDir, '.browser-profile'));
  const userDataDir = path.join(tmp, 'destino');
  assert.equal(migrateLegacyProfile({ projectDir, userDataDir }).migrated, true);
  assert.equal(migrateLegacyProfile({ projectDir, userDataDir }).migrated, false);
});

test('un fallo de migración no lanza excepción', () => {
  const projectDir = path.join(tmp, 'proyecto');
  makeProfile(path.join(projectDir, '.browser-profile'));
  // Ruta imposible: el padre es un archivo, no un directorio.
  const bloqueado = path.join(tmp, 'archivo.txt');
  fs.writeFileSync(bloqueado, 'x');
  const r = migrateLegacyProfile({ projectDir, userDataDir: path.join(bloqueado, 'sub') });
  assert.equal(r.migrated, false, 'devuelve el fallo en vez de reventar el login');
});

test('looksLikeProfile distingue perfil real de carpeta cualquiera', () => {
  assert.equal(looksLikeProfile(makeProfile(path.join(tmp, 'real'))), true);
  assert.equal(looksLikeProfile(path.join(tmp, 'no-existe')), false);
  fs.mkdirSync(path.join(tmp, 'vacia'));
  assert.equal(looksLikeProfile(path.join(tmp, 'vacia')), false);
  fs.writeFileSync(path.join(tmp, 'fichero'), 'x');
  assert.equal(looksLikeProfile(path.join(tmp, 'fichero')), false);
});

test('reconoce un perfil que solo tiene Default/', () => {
  const dir = path.join(tmp, 'solo-default');
  fs.mkdirSync(path.join(dir, 'Default'), { recursive: true });
  assert.equal(looksLikeProfile(dir), true);
});
