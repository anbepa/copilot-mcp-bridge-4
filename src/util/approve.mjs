/**
 * Aprobación humana interactiva. Muestra el diff EXACTO antes de tocar disco.
 * Modo --yes lo salta (solo para demos/CI: nunca contra código real).
 */
import readline from 'node:readline';
import fs from 'node:fs/promises';
import path from 'node:path';
import { unifiedDiff, colorizeDiff } from './diff.mjs';
import { log, color } from '../log.mjs';
import { realish } from './paths.mjs';
import { normalizeEdits, applyEdits } from './edits.mjs';

export function createApprover({ autoYes = false, roots = [] } = {}) {
  return async function approve(step, evaluation) {
    log.banner('APROBACIÓN REQUERIDA');
    process.stdout.write(
      `${color.bold('Acción:')} ${step.server}.${step.tool}  ${color.gray('(' + evaluation.kind + ')')}\n` +
        `${color.bold('Motivo:')} ${evaluation.reason}\n`
    );

    const preview = await buildPreview(step, roots);
    if (preview) process.stdout.write('\n' + preview + '\n');
    else process.stdout.write(`${color.bold('Args:')} ${JSON.stringify(step.args, null, 2)}\n`);

    if (autoYes) {
      log.warn('--yes activo: aprobado automáticamente');
      return true;
    }
    return await ask(`\n${color.bold('¿Aplicar?')} [${color.green('s')}í / ${color.red('n')}o] `);
  };
}

async function buildPreview(step, roots) {
  const root = roots[0];
  if (!root) return null;
  try {
    if (step.tool === 'write_file') {
      const abs = path.isAbsolute(step.args.path) ? step.args.path : path.join(realish(root), step.args.path);
      let before = '';
      try { before = await fs.readFile(abs, 'utf8'); } catch {}
      const d = unifiedDiff(before, step.args.content ?? '', { label: step.args.path });
      return colorizeDiff(d);
    }
    if (step.tool === 'edit_file') {
      const abs = path.isAbsolute(step.args.path) ? step.args.path : path.join(realish(root), step.args.path);
      const before = await fs.readFile(abs, 'utf8');
      // Misma normalización que el servidor MCP: la previsualización debe reflejar
      // lo que REALMENTE va a pasar, no una simulación con reglas distintas.
      const norm = normalizeEdits(step.args.edits);
      if (!norm.ok) return color.yellow(`⚠ ${norm.error.message} La operación fallará.`);
      const res = applyEdits(before, norm.edits);
      if (!res.ok) return color.yellow(`⚠ ${res.error.message} La operación fallará en ${step.args.path}.`);
      const diff = colorizeDiff(unifiedDiff(before, res.updated, { label: step.args.path }));
      return res.fuzzy ? `${color.yellow('⚠ Alguna ancla coincidió ignorando espacios en blanco.')}\n${diff}` : diff;
    }
    if (step.tool === 'move_file') return `${color.red('- ' + step.args.source)}\n${color.green('+ ' + step.args.destination)}`;
    if (step.tool === 'create_directory') return color.green('+ ' + step.args.path + '/');
  } catch (e) {
    return color.yellow(`(no se pudo generar la previsualización: ${e.message})`);
  }
  return null;
}

function ask(question, { raw = false } = {}) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(question, (a) => {
      rl.close();
      if (raw) return resolve(a);
      resolve(/^(s|si|sí|y|yes)$/i.test(a.trim()));
    });
  });
}

export { ask };
