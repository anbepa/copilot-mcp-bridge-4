/**
 * Regresión del fallo real observado en producción (log de Andres, 2026-08-11):
 * Copilot emitió {"search","replace"} en vez de {"oldText","newText"}; el servidor
 * devolvió ENOMATCH con el hint "vuelve a leer el archivo" y el modelo entró en
 * bucle releyendo un archivo que ya era correcto hasta agotar los turnos.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEdit, normalizeEdits, applyEdits, flexibleFind } from '../src/util/edits.mjs';

const FILE = '// TODO: mover credenciales a variables de entorno\nconst CONFIG = { host: "localhost" };\n';

test('acepta search/replace (el alias que usó Copilot en el fallo real)', () => {
  const r = normalizeEdit({ search: 'a', replace: 'b' });
  assert.equal(r.ok, true);
  assert.equal(r.edit.oldText, 'a');
  assert.equal(r.edit.newText, 'b');
});

test('acepta old_string/new_string y find/replaceWith', () => {
  assert.equal(normalizeEdit({ old_string: 'a', new_string: 'b' }).ok, true);
  assert.equal(normalizeEdit({ find: 'a', replaceWith: 'b' }).ok, true);
  assert.equal(normalizeEdit({ from: 'a', to: 'b' }).ok, true);
});

test('el flujo completo que antes fallaba ahora aplica la edición', () => {
  const norm = normalizeEdits([
    { search: '// TODO: mover credenciales a variables de entorno', replace: '// Credenciales desde process.env' }
  ]);
  assert.equal(norm.ok, true);
  const res = applyEdits(FILE, norm.edits);
  assert.equal(res.ok, true);
  assert.match(res.updated, /Credenciales desde process\.env/);
  assert.doesNotMatch(res.updated, /TODO: mover/);
});

test('campos irreconocibles dan EBADEDIT, NO ENOMATCH', () => {
  const r = normalizeEdit({ foo: 'a', bar: 'b' });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, 'EBADEDIT'); // diagnóstico correcto = no hay bucle
  assert.match(r.error.message, /foo, bar/); // dice qué llegó
  assert.match(r.error.hint, /oldText/);
});

test('el hint de EBADEDIT prohíbe explícitamente releer el archivo', () => {
  // Esta es LA causa del bucle infinito: el hint mandaba a repetir lo ya hecho.
  const r = normalizeEdit({ foo: 'a', bar: 'b' });
  assert.match(r.error.hint, /NO vuelvas a leer el archivo/i);
});

test('ENOMATCH real incluye la línea parecida del archivo como pista', () => {
  const res = applyEdits(FILE, [{ oldText: '// TODO: mover credenciales a otro sitio', newText: 'x' }]);
  assert.equal(res.ok, false);
  assert.equal(res.error.code, 'ENOMATCH');
  assert.match(res.error.hint, /línea 1/);
});

test('coincidencia tolerante a diferencias de indentación', () => {
  const src = 'function f() {\n      return 1;\n}\n';
  const res = applyEdits(src, [{ oldText: 'function f() {\n  return 1;\n}', newText: 'function f() {\n  return 2;\n}' }]);
  assert.equal(res.ok, true);
  assert.equal(res.fuzzy, 1);
  assert.match(res.updated, /return 2/);
});

test('la coincidencia exacta tiene prioridad y no marca fuzzy', () => {
  const res = applyEdits(FILE, [{ oldText: 'const CONFIG = { host: "localhost" };', newText: 'const CONFIG = {};' }]);
  assert.equal(res.ok, true);
  assert.equal(res.fuzzy, 0);
});

test('ancla ambigua se rechaza en vez de editar la ocurrencia equivocada', () => {
  const src = 'let a = 1;\nlet a = 1;\n';
  const res = applyEdits(src, [{ oldText: 'let a = 1;', newText: 'let a = 2;' }]);
  assert.equal(res.ok, false);
  assert.equal(res.error.code, 'EAMBIGUOUS');
});

test('oldText vacío se rechaza (borraría el archivo por accidente)', () => {
  const r = normalizeEdit({ oldText: '', newText: 'x' });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, 'EBADEDIT');
});

test('newText con $& no se interpreta como patrón de reemplazo', () => {
  // Bug clásico de String.replace: "$&" insertaría la coincidencia entera.
  const res = applyEdits('valor: A\n', [{ oldText: 'A', newText: '$& $1 $$' }]);
  assert.equal(res.ok, true);
  assert.match(res.updated, /valor: \$& \$1 \$\$/);
});

test('ediciones múltiples se aplican en secuencia', () => {
  const res = applyEdits('uno\ndos\n', [
    { oldText: 'uno', newText: '1' },
    { search: 'dos', replace: '2' }
  ]);
  assert.equal(res.ok, true);
  assert.equal(res.applied.length, 2);
  assert.equal(res.updated, '1\n2\n');
});

test('edits no-array se rechaza con formato de ejemplo', () => {
  const r = normalizeEdits('no soy un array');
  assert.equal(r.ok, false);
  assert.match(r.error.hint, /oldText/);
});

test('flexibleFind detecta ausencia real', () => {
  assert.equal(flexibleFind('hola mundo', 'texto inexistente'), null);
});
