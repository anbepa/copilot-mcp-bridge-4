/**
 * Validación de estructuras del protocolo. Cero dependencias (sin zod).
 * Devuelve errores en lenguaje natural, pensados para reinyectarse al modelo
 * como pista de autocorrección.
 */

export function validatePlan(v, { maxSteps = 20 } = {}) {
  const errors = [];
  if (!v || typeof v !== 'object') return { ok: false, errors: ['El plan debe ser un objeto JSON'] };
  if (!Array.isArray(v.steps)) return { ok: false, errors: ['Falta el array "steps"'] };
  if (v.steps.length === 0) return { ok: false, errors: ['"steps" está vacío. Si no necesitas herramientas, emite ```mcp-done'] };
  if (v.steps.length > maxSteps) errors.push(`El plan tiene ${v.steps.length} pasos; el máximo es ${maxSteps}`);

  const ids = new Set();
  const normalized = [];
  v.steps.forEach((s, i) => {
    const where = `paso #${i + 1}`;
    if (!s || typeof s !== 'object') {
      errors.push(`${where}: debe ser un objeto`);
      return;
    }
    const id = String(s.id ?? `s${i + 1}`);
    if (ids.has(id)) errors.push(`${where}: id duplicado "${id}"`);
    ids.add(id);
    if (!s.tool || typeof s.tool !== 'string') errors.push(`${where} (${id}): falta "tool"`);
    const server = s.server ?? s.serverName ?? null;
    const args = s.args ?? s.arguments ?? {};
    if (args !== null && typeof args !== 'object') errors.push(`${where} (${id}): "args" debe ser objeto`);
    const deps = s.depends_on ?? s.dependsOn ?? [];
    if (!Array.isArray(deps)) errors.push(`${where} (${id}): "depends_on" debe ser un array de ids`);
    normalized.push({ id, server, tool: s.tool, args: args ?? {}, depends_on: deps.map(String) });
  });

  for (const s of normalized) {
    for (const d of s.depends_on) {
      if (!ids.has(d)) errors.push(`paso "${s.id}": depende de "${d}", que no existe en el plan`);
    }
  }

  const cycle = findCycle(normalized);
  if (cycle) errors.push(`Ciclo de dependencias detectado: ${cycle.join(' → ')}`);

  return errors.length ? { ok: false, errors } : { ok: true, value: { steps: normalized, then: v.then ?? null } };
}

export function validateAction(v) {
  const errors = [];
  if (!v || typeof v !== 'object') return { ok: false, errors: ['La acción debe ser un objeto JSON'] };
  if (!v.tool) errors.push('Falta "tool"');
  const args = v.args ?? v.arguments ?? {};
  if (typeof args !== 'object') errors.push('"args" debe ser objeto');
  return errors.length
    ? { ok: false, errors }
    : { ok: true, value: { id: String(v.id ?? 'a1'), server: v.server ?? null, tool: v.tool, args, depends_on: [] } };
}

function findCycle(steps) {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const state = new Map();
  const stack = [];
  let cycle = null;

  function visit(id) {
    if (cycle) return;
    const st = state.get(id);
    if (st === 'done') return;
    if (st === 'visiting') {
      cycle = [...stack.slice(stack.indexOf(id)), id];
      return;
    }
    state.set(id, 'visiting');
    stack.push(id);
    for (const d of byId.get(id)?.depends_on ?? []) if (byId.has(d)) visit(d);
    stack.pop();
    state.set(id, 'done');
  }
  for (const s of steps) visit(s.id);
  return cycle;
}

/** Orden topológico en oleadas → cada oleada se ejecuta en paralelo. */
export function toWaves(steps) {
  const remaining = new Map(steps.map((s) => [s.id, s]));
  const done = new Set();
  const waves = [];
  let guard = 0;
  while (remaining.size > 0 && guard++ < 100) {
    const wave = [...remaining.values()].filter((s) => s.depends_on.every((d) => done.has(d) || !remaining.has(d)));
    if (wave.length === 0) break; // ciclo residual
    for (const s of wave) {
      remaining.delete(s.id);
      done.add(s.id);
    }
    waves.push(wave);
  }
  if (remaining.size > 0) waves.push([...remaining.values()]);
  return waves;
}
