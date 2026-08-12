import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseReply, extractFences, parseLoose } from '../src/protocol/blocks.mjs';
import { validatePlan, toWaves } from '../src/protocol/validate.mjs';

test('extrae bloque mcp-plan limpio', () => {
  const r = parseReply('```mcp-plan\n{"steps":[{"id":"s1","server":"fs","tool":"grep","args":{}}]}\n```');
  assert.equal(r.kind, 'plan');
  assert.equal(r.value.steps.length, 1);
});

test('ignora prosa alrededor del bloque', () => {
  const r = parseReply('Claro, aquí tienes:\n\n```mcp-plan\n{"steps":[{"id":"a","tool":"grep"}]}\n```\n\n¡Espero que ayude!');
  assert.equal(r.kind, 'plan');
  assert.match(r.prose, /Claro/);
});

test('repara comas colgantes y comillas tipográficas', () => {
  const r = parseReply('```mcp-plan\n{"steps":[{"id":"s1","tool":"grep",}],}\n```');
  assert.equal(r.kind, 'plan');
});

test('repara comentarios // dentro del JSON', () => {
  const r = parseReply('```mcp-plan\n{\n// primero busco\n"steps":[{"id":"s1","tool":"grep"}]\n}\n```');
  assert.equal(r.kind, 'plan');
});

test('infiere el tipo cuando el modelo usa ```json', () => {
  const r = parseReply('```json\n{"steps":[{"id":"s1","tool":"grep"}]}\n```');
  assert.equal(r.kind, 'plan');
  assert.equal(r.inferred, true);
});

test('detecta mcp-done', () => {
  const r = parseReply('```mcp-done\n{"summary":"listo","files_changed":["a.js"]}\n```');
  assert.equal(r.kind, 'done');
  assert.equal(r.value.summary, 'listo');
});

test('detecta mcp-ask', () => {
  const r = parseReply('```mcp-ask\n{"question":"¿sobrescribo?"}\n```');
  assert.equal(r.kind, 'ask');
});

test('respuesta sin bloque devuelve none', () => {
  const r = parseReply('No entendí la tarea, ¿puedes aclarar?');
  assert.equal(r.kind, 'none');
});

test('no confunde bloques de código normales con protocolo', () => {
  const r = parseReply('Ejemplo:\n```javascript\nconst x = 1;\n```\nnada más');
  assert.equal(r.kind, 'none');
});

test('soporta vallas de 4 backticks con bloque anidado', () => {
  const fences = extractFences('````mcp-plan\n{"steps":[]}\n````');
  assert.equal(fences.length, 1);
  assert.equal(fences[0].lang, 'mcp-plan');
});

test('parseLoose recorta texto sobrante tras el JSON', () => {
  const r = parseLoose('{"a":1} y algo más de texto');
  assert.equal(r.ok, true);
  assert.equal(r.value.a, 1);
});

test('validatePlan normaliza ids y args ausentes', () => {
  const v = validatePlan({ steps: [{ tool: 'grep' }, { tool: 'read_text_file' }] });
  assert.equal(v.ok, true);
  assert.equal(v.value.steps[0].id, 's1');
  assert.deepEqual(v.value.steps[0].args, {});
});

test('validatePlan acepta arguments/dependsOn como alias', () => {
  const v = validatePlan({ steps: [{ id: 'a', tool: 't', arguments: { x: 1 } }, { id: 'b', tool: 't', dependsOn: ['a'] }] });
  assert.equal(v.ok, true);
  assert.deepEqual(v.value.steps[0].args, { x: 1 });
  assert.deepEqual(v.value.steps[1].depends_on, ['a']);
});

test('validatePlan rechaza dependencia inexistente', () => {
  const v = validatePlan({ steps: [{ id: 'a', tool: 't', depends_on: ['zzz'] }] });
  assert.equal(v.ok, false);
  assert.match(v.errors.join(), /zzz/);
});

test('validatePlan detecta ciclos', () => {
  const v = validatePlan({ steps: [{ id: 'a', tool: 't', depends_on: ['b'] }, { id: 'b', tool: 't', depends_on: ['a'] }] });
  assert.equal(v.ok, false);
  assert.match(v.errors.join(), /[Cc]iclo/);
});

test('validatePlan rechaza plan vacío con pista útil', () => {
  const v = validatePlan({ steps: [] });
  assert.equal(v.ok, false);
  assert.match(v.errors.join(), /mcp-done/);
});

test('toWaves agrupa pasos independientes en una sola oleada', () => {
  const steps = [
    { id: 'a', tool: 't', depends_on: [] },
    { id: 'b', tool: 't', depends_on: [] },
    { id: 'c', tool: 't', depends_on: ['a', 'b'] }
  ];
  const waves = toWaves(steps);
  assert.equal(waves.length, 2);
  assert.equal(waves[0].length, 2); // a y b en paralelo
  assert.equal(waves[1][0].id, 'c');
});
