import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PolicyEngine, Decision } from '../src/policy/engine.mjs';
import { isInside, globToRegExp, matchesAnyGlob } from '../src/util/paths.mjs';
import { truncateBytes, truncateHeadTail } from '../src/util/truncate.mjs';
import { unifiedDiff } from '../src/util/diff.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'workspace');

function engine(overrides = {}) {
  return new PolicyEngine({
    policy: {
      autoApproveReads: true,
      requireApprovalForWrites: true,
      allowedServers: ['fs'],
      deniedTools: ['shell'],
      maxWritesPerTask: 3,
      ...overrides
    },
    sandbox: { denyGlobs: ['**/.env', '**/*.key', '**/secrets/**'] },
    roots: [ROOT]
  });
}

test('permite lectura dentro del sandbox', () => {
  const r = engine().evaluate({ server: 'fs', tool: 'read_text_file', args: { path: 'src/users.js' } });
  assert.equal(r.decision, Decision.ALLOW);
});

test('bloquea traversal con ..', () => {
  const r = engine().evaluate({ server: 'fs', tool: 'read_text_file', args: { path: '../../../etc/passwd' } });
  assert.equal(r.decision, Decision.DENY);
});

test('bloquea ruta absoluta fuera del root', () => {
  const r = engine().evaluate({ server: 'fs', tool: 'read_text_file', args: { path: '/etc/hosts' } });
  assert.equal(r.decision, Decision.DENY);
});

test('bloquea archivos sensibles por glob', () => {
  for (const p of ['.env', 'config/prod.key', 'secrets/token.txt']) {
    const r = engine().evaluate({ server: 'fs', tool: 'read_text_file', args: { path: p } });
    assert.equal(r.decision, Decision.DENY, `debería bloquear ${p}`);
  }
});

test('bloquea herramientas de ejecución aunque no estén en deniedTools', () => {
  const r = engine({ deniedTools: [] }).evaluate({ server: 'fs', tool: 'bash', args: {} });
  assert.equal(r.decision, Decision.DENY);
});

test('bloquea servidores fuera de la allowlist', () => {
  const r = engine().evaluate({ server: 'malicioso', tool: 'read_text_file', args: { path: 'a.js' } });
  assert.equal(r.decision, Decision.DENY);
});

test('escritura pide aprobación', () => {
  const r = engine().evaluate({ server: 'fs', tool: 'write_file', args: { path: 'nuevo.js', content: 'x' } });
  assert.equal(r.decision, Decision.ASK);
  assert.equal(r.kind, 'write');
});

test('respeta la cuota de escrituras por tarea', () => {
  const e = engine();
  for (let i = 0; i < 3; i++) e.noteWrite();
  const r = e.evaluate({ server: 'fs', tool: 'write_file', args: { path: 'a.js', content: 'x' } });
  assert.equal(r.decision, Decision.DENY);
});

test('valida rutas anidadas en args complejos (move_file)', () => {
  const r = engine().evaluate({ server: 'fs', tool: 'move_file', args: { source: 'a.js', destination: '../../fuera.js' } });
  assert.equal(r.decision, Decision.DENY);
});

test('isInside rechaza escapes y acepta descendientes', () => {
  assert.equal(isInside(ROOT, path.join(ROOT, 'src', 'a.js')), true);
  assert.equal(isInside(ROOT, path.join(ROOT, '..', 'otro')), false);
});

test('globToRegExp maneja ** y *', () => {
  assert.equal(globToRegExp('**/*.key').test('a/b/c.key'), true);
  assert.equal(globToRegExp('*.key').test('a/b.key'), false);
  assert.equal(matchesAnyGlob('deep/nested/.env', ['**/.env']), true);
});

test('truncateBytes no rompe caracteres multibyte', () => {
  const s = 'áéíóú'.repeat(100);
  const r = truncateBytes(s, 51);
  assert.equal(r.truncated, true);
  assert.doesNotThrow(() => Buffer.from(r.text, 'utf8').toString('utf8'));
  assert.ok(!r.text.includes('\uFFFD'));
});

test('truncateHeadTail conserva principio y final', () => {
  const s = Array.from({ length: 500 }, (_, i) => `linea ${i}`).join('\n');
  const r = truncateHeadTail(s, 400);
  assert.equal(r.truncated, true);
  assert.match(r.text, /linea 0/);
  assert.match(r.text, /linea 499/);
});

test('unifiedDiff produce hunks correctos', () => {
  const d = unifiedDiff('a\nb\nc\n', 'a\nB\nc\n', { label: 'f.js' });
  assert.match(d, /-b/);
  assert.match(d, /\+B/);
  assert.match(d, /@@/);
});

test('unifiedDiff sin cambios', () => {
  assert.equal(unifiedDiff('a\nb', 'a\nb'), '(sin cambios)');
});
