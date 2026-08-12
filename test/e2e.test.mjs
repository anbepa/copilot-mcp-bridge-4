/**
 * E2E del bucle completo con MockDriver: Context Pack → plan → ejecución
 * paralela → política → escritura → done. Sin navegador.
 */
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { McpHost } from '../src/mcp/host.mjs';
import { PolicyEngine } from '../src/policy/engine.mjs';
import { Orchestrator } from '../src/orchestrator/loop.mjs';
import { ContextCompiler } from '../src/context/compiler.mjs';
import { MockDriver } from '../src/driver/mock.mjs';
import { Audit } from '../src/audit.mjs';
import { setLevel } from '../src/log.mjs';

setLevel('silent');

const PROJECT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let tmpRoot, host, audit;

const CFG = {
  budget: { maxTurns: 8, maxStepsPerPlan: 20, maxBytesPerResult: 8000, maxTotalResultBytes: 120000, maxTaskSeconds: 60, parallelism: 4, maxRepairAttempts: 2 },
  context: { maxTreeEntries: 100, maxSymbolFiles: 50, maxKeyFileBytes: 4000, keyFiles: ['package.json'], codeExtensions: ['.js'], maxPackBytes: 30000 }
};

before(async () => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cmb-e2e-'));
  fs.mkdirSync(path.join(tmpRoot, 'src'), { recursive: true });
  fs.writeFileSync(path.join(tmpRoot, 'src', 'users.js'), '// TODO: validar entrada del id\nexport async function getUser(db, id) { return db.query(id); }\n');
  fs.writeFileSync(path.join(tmpRoot, 'src', 'index.js'), '// TODO: manejar errores de conexion\nexport async function main() {}\n');
  fs.writeFileSync(path.join(tmpRoot, 'package.json'), '{"name":"e2e"}');

  host = new McpHost({
    servers: { fs: { command: 'node', args: ['./src/mcp/servers/fs-server.mjs', '{{ROOTS}}'] } },
    roots: [tmpRoot],
    cwd: PROJECT
  });
  await host.start();
  audit = new Audit({ dir: path.join(tmpRoot, '.audit'), enabled: true });
});

after(async () => {
  await host?.stop();
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

function makeOrchestrator(driver, { approveAll = true } = {}) {
  const policy = new PolicyEngine({
    policy: { autoApproveReads: true, requireApprovalForWrites: true, allowedServers: ['fs'], deniedTools: [], maxWritesPerTask: 10 },
    sandbox: { denyGlobs: ['**/.env'] },
    roots: [tmpRoot]
  });
  return new Orchestrator({
    driver, host, policy,
    config: CFG,
    approver: async () => approveAll,
    audit,
    contextPack: { pack: '## CONTEXTO\n(vacío para el test)', attachmentPath: null, attachmentName: null },
    roots: [tmpRoot]
  });
}

test('ContextCompiler produce árbol, símbolos y TODOs', async () => {
  const c = new ContextCompiler({ root: tmpRoot, config: CFG.context, denyGlobs: [] });
  const r = await c.compile({ task: 'documentar TODO' });
  assert.match(r.pack, /ÁRBOL DEL PROYECTO/);
  assert.match(r.pack, /MAPA DE SÍMBOLOS/);
  assert.match(r.pack, /getUser/);
  assert.match(r.pack, /TODO/);
  assert.ok(r.manifest['src/users.js']?.hash);
});

test('ContextCompiler marca archivos sin cambios usando el manifiesto previo', async () => {
  const c = new ContextCompiler({ root: tmpRoot, config: CFG.context, denyGlobs: [] });
  const first = await c.compile({ task: 'x' });
  const second = await c.compile({ task: 'x', previousManifest: first.manifest });
  assert.ok(second.stats.unchanged > 0);
  assert.match(second.pack, /SIN CAMBIOS/);
});

test('bucle completo llega a done y escribe en disco', async () => {
  const orch = makeOrchestrator(new MockDriver({ mode: 'smart', latencyMs: 0 }));
  const res = await orch.run('Documenta los TODO de src/');
  assert.equal(res.status, 'done');
  assert.ok(res.budget.turns <= 4, `esperaba <=4 turnos, hubo ${res.budget.turns}`);
  const content = fs.readFileSync(path.join(tmpRoot, 'src', 'users.js'), 'utf8');
  assert.match(content, /entero positivo/);
  assert.ok(!content.includes('TODO: validar'));
});

test('el orquestador se recupera de formato inválido y luego completa', async () => {
  let n = 0;
  const flaky = {
    async init() {},
    async send() {
      n++;
      if (n === 1) return 'Perdón, no estoy seguro de qué hacer aquí.';
      return '```mcp-done\n{"summary":"ok tras reparación"}\n```';
    },
    async close() {}
  };
  const res = await makeOrchestrator(flaky).run('tarea');
  assert.equal(res.status, 'done');
  assert.equal(n, 2);
});

test('agota los reintentos de reparación y termina con parse_error', async () => {
  const broken = { async init() {}, async send() { return 'bla bla sin bloque'; }, async close() {} };
  const res = await makeOrchestrator(broken).run('tarea');
  assert.equal(res.status, 'parse_error');
});

test('respeta el límite de turnos', async () => {
  const looper = {
    async init() {},
    async send() { return '```mcp-plan\n{"steps":[{"id":"s1","server":"fs","tool":"grep","args":{"path":".","pattern":"x"}}]}\n```'; },
    async close() {}
  };
  const res = await makeOrchestrator(looper).run('bucle infinito');
  assert.equal(res.status, 'budget');
  assert.equal(res.budget.turns, CFG.budget.maxTurns);
});

test('un rechazo del usuario no aborta el bucle y se reporta al modelo', async () => {
  const orch = makeOrchestrator(new MockDriver({ mode: 'smart', latencyMs: 0 }), { approveAll: false });
  const res = await orch.run('Documenta los TODO');
  assert.equal(res.status, 'done');
  assert.equal(res.filesChanged.length, 0);
});

test('mcp-ask detiene el bucle pidiendo decisión', async () => {
  const asker = {
    async init() {},
    async send() { return '```mcp-ask\n{"question":"¿Sobrescribo config?"}\n```'; },
    async close() {}
  };
  const res = await makeOrchestrator(asker).run('tarea');
  assert.equal(res.status, 'ask');
  assert.match(res.detail, /Sobrescribo/);
});

test('la política bloquea el paso fuera del sandbox dentro de un plan válido', async () => {
  const evil = {
    n: 0,
    async init() {},
    async send() {
      this.n++;
      if (this.n === 1) {
        return '```mcp-plan\n{"steps":[{"id":"x","server":"fs","tool":"read_text_file","args":{"path":"../../../etc/passwd"}}]}\n```';
      }
      return '```mcp-done\n{"summary":"bloqueado correctamente"}\n```';
    },
    async close() {}
  };
  const res = await makeOrchestrator(evil).run('leer passwd');
  assert.equal(res.status, 'done');
});
