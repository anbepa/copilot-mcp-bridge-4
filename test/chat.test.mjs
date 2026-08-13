/**
 * Tests del MODO CHAT (REPL conversacional) del Orchestrator.
 *
 * No requieren navegador ni MCP real: usamos un driver falso (que imita a
 * Copilot devolviendo bloques mcp-*) y un host/policy mínimos en memoria.
 * El foco es el NUEVO comportamiento: encadenar varios mensajes del usuario,
 * reutilizar el hilo, reiniciar el presupuesto por mensaje y acumular cambios.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Orchestrator } from '../src/orchestrator/loop.mjs';
import { setLevel } from '../src/log.mjs';

setLevel('silent');

const CFG = {
  interactive: true,
  budget: {
    maxTurns: 4, maxStepsPerPlan: 20, maxBytesPerResult: 8000,
    maxTotalResultBytes: 120000, maxTaskSeconds: 60, parallelism: 4, maxRepairAttempts: 2
  }
};

/** Host falso: un solo "servidor" sin herramientas reales. */
function fakeHost() {
  return {
    clients: new Map([['unified', {}]]),
    catalogForPrompt: () => ['unified.read_file(path)'],
    catalog: () => []
  };
}

/** Policy falsa: nada es escritura, nada se bloquea. */
function fakePolicy() {
  return {
    classify: () => 'read',
    evaluate: () => ({ decision: 'allow' })
  };
}

/** Audit falso (no-op). */
function fakeAudit() {
  return { record() {}, file: null };
}

/**
 * PlanExecutor real haría llamadas MCP; aquí lo cortocircuitamos monkey-patcheando
 * el executor del orquestador para devolver resultados vacíos "ok".
 */
function neutralizeExecutor(orch) {
  orch.executor = {
    defaultServer: 'unified',
    budget: orch.budget,
    async execute(steps) {
      return { results: steps.map((s) => ({ id: s.id, ok: true, value: {} })) };
    }
  };
}

function makeOrchestrator(driver) {
  const orch = new Orchestrator({
    driver,
    host: fakeHost(),
    policy: fakePolicy(),
    config: CFG,
    approver: async () => true,
    audit: fakeAudit(),
    contextPack: { pack: '## CTX', attachmentPath: null, attachmentName: null },
    roots: ['/tmp/fake']
  });
  neutralizeExecutor(orch);
  return orch;
}

/** Convierte una lista de mensajes de usuario en una función promptUser(). */
function scriptedUser(messages) {
  let i = 0;
  const asked = [];
  const fn = async () => {
    const m = messages[i++] ?? 'salir';
    asked.push(m);
    return m;
  };
  fn.asked = asked;
  return fn;
}

test('chat: encadena varios mensajes del usuario y termina con "salir"', async () => {
  // El driver siempre cierra la tarea con done.
  const driver = {
    prompts: [],
    async init() {},
    async send(msg) { this.prompts.push(msg); return '```mcp-done\n{"summary":"listo"}\n```'; },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const user = scriptedUser(['primera tarea', 'segunda tarea', 'salir']);

  const res = await orch.runChat({ promptUser: user });

  // Se procesaron 2 tareas reales (la 3ª fue "salir").
  assert.equal(res.tasks, 2, `esperaba 2 tareas, hubo ${res.tasks}`);
  assert.equal(res.status, 'done');
});

test('chat: el bootstrap solo viaja en el 1.er mensaje; los siguientes usan mcp-user', async () => {
  const driver = {
    prompts: [],
    async init() {},
    async send(msg) { this.prompts.push(msg); return '```mcp-done\n{"summary":"ok"}\n```'; },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const user = scriptedUser(['hola', 'otra cosa', 'salir']);

  await orch.runChat({ promptUser: user });

  // Primer prompt: contrato + catálogo (bootstrap).
  assert.match(driver.prompts[0], /planificador de un puente local/);
  // Segundo prompt: bloque mcp-user con la instrucción, SIN reenviar el contrato.
  assert.match(driver.prompts[1], /```mcp-user/);
  assert.ok(!/planificador de un puente local/.test(driver.prompts[1]),
    'el 2.º mensaje NO debe reenviar el contrato completo');
});

test('chat: firstTask se ejecuta sin pedir el primer mensaje', async () => {
  const driver = {
    async init() {},
    async send() { return '```mcp-done\n{"summary":"ok"}\n```'; },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const user = scriptedUser(['salir']); // solo debería pedirse UNA vez (para el 2.º turno)

  const res = await orch.runChat({ firstTask: 'tarea precargada', promptUser: user });

  assert.equal(res.tasks, 1);
  // promptUser se invocó una sola vez y devolvió "salir".
  assert.deepEqual(user.asked, ['salir']);
});

test('chat: el presupuesto de turnos se REINICIA por cada mensaje (no se agota)', async () => {
  // Cada tarea consume varios turnos de plan antes de done. Con maxTurns=4,
  // si NO se reiniciara, la 2.ª tarea empezaría ya agotada. Verificamos que
  // ambas llegan a "done".
  let turnInTask = 0;
  const driver = {
    async init() {},
    async send(msg) {
      // Un plan, luego done: 2 turnos por tarea.
      if (/```mcp-results/.test(msg)) { turnInTask = 0; return '```mcp-done\n{"summary":"cerrada"}\n```'; }
      turnInTask++;
      return '```mcp-plan\n{"steps":[{"id":"s1","server":"unified","tool":"read_file","args":{"path":"a.js"}}]}\n```';
    },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const user = scriptedUser(['t1', 't2', 't3', 'salir']);

  const res = await orch.runChat({ promptUser: user });

  assert.equal(res.tasks, 3, 'las 3 tareas deben completarse pese al maxTurns bajo');
  assert.equal(res.status, 'done');
});

test('chat: promptUser que devuelve cadena vacía también termina la sesión', async () => {
  const driver = {
    async init() {},
    async send() { return '```mcp-done\n{"summary":"ok"}\n```'; },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const user = scriptedUser(['una tarea', '']); // "" = terminar

  const res = await orch.runChat({ promptUser: user });
  assert.equal(res.tasks, 1);
});

test('chat: si no se pasa promptUser, lanza error claro', async () => {
  const driver = { async init() {}, async send() { return ''; }, async close() {} };
  const orch = makeOrchestrator(driver);
  await assert.rejects(() => orch.runChat({}), /promptUser/);
});

test('runTask sigue devolviendo el resultado clásico (compatibilidad)', async () => {
  const driver = {
    async init() {},
    async send() { return '```mcp-done\n{"summary":"tarea única"}\n```'; },
    async close() {}
  };
  const orch = makeOrchestrator(driver);
  const res = await orch.run('haz algo'); // API pública estable
  assert.equal(res.status, 'done');
  assert.match(res.detail, /tarea única/);
});
