#!/usr/bin/env node
/**
 * CALIBRACIÓN — ejecútalo ANTES de usar el puente en serio.
 *
 * Mide los 4 números de los que depende todo el diseño y que varían por tenant,
 * versión de UI y modelo:
 *   1. Límite real de caracteres del composer
 *   2. Si los adjuntos funcionan y su tamaño máximo útil
 *   3. Latencia media por turno
 *   4. Fiabilidad del formato ```mcp-*  (¿respeta el contrato?)
 *
 * Escribe los resultados en config/calibration.json.
 */
import fs from 'node:fs';
import path from 'node:path';
import { loadConfig, ROOT_DIR } from '../src/config.mjs';
import { PlaywrightDriver } from '../src/driver/playwright.mjs';
import { parseReply } from '../src/protocol/blocks.mjs';
import { log, color } from '../src/log.mjs';

const cfg = loadConfig({ driver: { headless: false } });
const results = { timestamp: new Date().toISOString(), model: cfg.driver.model, url: cfg.driver.url };

const driver = new PlaywrightDriver(cfg.driver);
await driver.init();

try {
  // ── 1. Latencia y adherencia al formato ──
  log.banner('1 · LATENCIA Y ADHERENCIA AL FORMATO');
  const latencies = [];
  let formatOk = 0;
  const N = 3;
  for (let i = 1; i <= N; i++) {
    const prompt = [
      'Responde EXCLUSIVAMENTE con este bloque, sin ninguna palabra adicional:',
      '```mcp-done',
      `{ "summary": "prueba ${i}" }`,
      '```'
    ].join('\n');
    const t0 = Date.now();
    const reply = await driver.send(prompt);
    const ms = Date.now() - t0;
    latencies.push(ms);
    const p = parseReply(reply);
    const ok = p.kind === 'done' && !p.inferred && !p.prose;
    if (ok) formatOk++;
    log.info(`intento ${i}: ${ms}ms · formato ${ok ? color.green('OK') : color.yellow(p.kind + (p.prose ? ' + prosa' : ''))}`);
  }
  results.latency = {
    samples: latencies,
    avgMs: Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length),
    minMs: Math.min(...latencies),
    maxMs: Math.max(...latencies)
  };
  results.formatAdherence = `${formatOk}/${N}`;

  // ── 2. Límite de caracteres del composer ──
  log.banner('2 · LÍMITE DEL COMPOSER');
  const sizes = [2000, 4000, 8000, 16000, 32000];
  let maxOk = 0;
  for (const size of sizes) {
    const filler = 'x'.repeat(size - 120);
    const prompt = `Ignora este relleno: ${filler}\n\nResponde solo:\n\`\`\`mcp-done\n{ "summary": "${size}" }\n\`\`\``;
    try {
      const reply = await driver.send(prompt);
      const p = parseReply(reply);
      const ok = p.kind === 'done' && String(p.value?.summary ?? '').includes(String(size));
      log.info(`${size} chars → ${ok ? color.green('OK') : color.red('FALLÓ')}`);
      if (ok) maxOk = size;
      else break;
    } catch (e) {
      log.warn(`${size} chars → error: ${e.message}`);
      break;
    }
  }
  results.composerMaxChars = maxOk;
  results.composerNote = maxOk === sizes.at(-1) ? `>= ${maxOk} (no se encontró el techo)` : `techo entre ${maxOk} y el siguiente escalón`;

  // ── 3. Adjuntos ──
  log.banner('3 · CANAL DE ADJUNTOS');
  const tmpDir = path.join(ROOT_DIR, '.tmp');
  fs.mkdirSync(tmpDir, { recursive: true });
  const probe = path.join(tmpDir, 'calibration-probe.md');
  const marker = 'MARCA_' + Math.random().toString(36).slice(2, 10).toUpperCase();
  fs.writeFileSync(probe, `# Documento de prueba\n\nEl código secreto es ${marker}.\n\n${'relleno. '.repeat(2000)}`);
  try {
    const reply = await driver.send(
      'Lee el archivo adjunto y responde SOLO con:\n```mcp-done\n{ "summary": "<el código secreto que aparece en el archivo>" }\n```',
      { attachment: probe }
    );
    const p = parseReply(reply);
    const found = JSON.stringify(p.value ?? '').includes(marker);
    results.attachments = { supported: found, probeBytes: fs.statSync(probe).size };
    log.info(`adjuntos: ${found ? color.green('FUNCIONAN') : color.yellow('no verificados')}`);
    if (!found) log.warn('El modelo no leyó el marcador. Usa --no-attach y el contexto irá en el prompt.');
  } catch (e) {
    results.attachments = { supported: false, error: e.message };
    log.warn(`adjuntos: fallo — ${e.message}`);
  }

  // ── Informe ──
  const outFile = path.join(ROOT_DIR, 'config', 'calibration.json');
  fs.writeFileSync(outFile, JSON.stringify(results, null, 2));
  log.banner('RESULTADOS');
  process.stdout.write(JSON.stringify(results, null, 2) + '\n');
  log.ok(`Guardado en ${outFile}`);

  log.banner('RECOMENDACIONES');
  const rec = [];
  if (results.attachments?.supported) rec.push('✓ Mantén attachments.enabled = true: es tu canal de mayor ancho de banda.');
  else rec.push('→ Pon attachments.enabled = false y baja context.maxPackBytes por debajo del límite del composer.');
  if (results.composerMaxChars) rec.push(`→ Ajusta context.maxPackBytes a ~${Math.floor(results.composerMaxChars * 0.7)} si no usas adjuntos.`);
  if (results.latency.avgMs > 12000) rec.push('⚠ Latencia alta: reduce budget.maxTurns y agrupa más pasos por plan.');
  if (formatOk < N) rec.push('⚠ El modelo no siempre respeta el formato: sube budget.maxRepairAttempts a 3.');
  process.stdout.write(rec.join('\n') + '\n');
} finally {
  await driver.close();
}
