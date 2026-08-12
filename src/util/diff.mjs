/** Diff unificado mínimo (LCS por líneas) para mostrar al usuario antes de aprobar escrituras. */
import { color } from '../log.mjs';

export function unifiedDiff(oldText, newText, { context = 3, label = 'archivo' } = {}) {
  const a = oldText.split('\n');
  const b = newText.split('\n');
  const ops = diffLines(a, b);

  const hunks = [];
  let cur = null;
  let ai = 0, bi = 0;

  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    if (op.type === 'eq') {
      if (cur) {
        cur.trailing = (cur.trailing ?? 0) + 1;
        cur.lines.push({ t: ' ', s: op.line });
        if (cur.trailing > context * 2) {
          cur.lines.splice(cur.lines.length - (cur.trailing - context), cur.trailing - context);
          hunks.push(cur);
          cur = null;
        }
      }
      ai++; bi++;
      continue;
    }
    if (!cur) {
      const startA = Math.max(0, ai - context);
      cur = { aStart: startA + 1, bStart: Math.max(0, bi - context) + 1, lines: [], trailing: 0 };
      for (let k = startA; k < ai; k++) cur.lines.push({ t: ' ', s: a[k] });
    }
    cur.trailing = 0;
    if (op.type === 'del') { cur.lines.push({ t: '-', s: op.line }); ai++; }
    else { cur.lines.push({ t: '+', s: op.line }); bi++; }
  }
  if (cur) hunks.push(cur);
  if (hunks.length === 0) return '(sin cambios)';

  const out = [`--- a/${label}`, `+++ b/${label}`];
  for (const h of hunks) {
    const aCount = h.lines.filter((l) => l.t !== '+').length;
    const bCount = h.lines.filter((l) => l.t !== '-').length;
    out.push(`@@ -${h.aStart},${aCount} +${h.bStart},${bCount} @@`);
    for (const l of h.lines) out.push(l.t + l.s);
  }
  return out.join('\n');
}

export function colorizeDiff(diff) {
  return diff
    .split('\n')
    .map((l) => {
      if (l.startsWith('+++') || l.startsWith('---')) return color.bold(l);
      if (l.startsWith('@@')) return color.cyan(l);
      if (l.startsWith('+')) return color.green(l);
      if (l.startsWith('-')) return color.red(l);
      return color.gray(l);
    })
    .join('\n');
}

/** LCS clásico; suficiente para archivos de código de tamaño normal. */
function diffLines(a, b) {
  const n = a.length, m = b.length;
  // Salvaguarda de memoria en archivos enormes: degradar a "todo borrado + todo añadido".
  if (n * m > 4_000_000) {
    return [...a.map((line) => ({ type: 'del', line })), ...b.map((line) => ({ type: 'add', line }))];
  }
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ type: 'eq', line: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: 'del', line: a[i] }); i++; }
    else { ops.push({ type: 'add', line: b[j] }); j++; }
  }
  while (i < n) ops.push({ type: 'del', line: a[i++] });
  while (j < m) ops.push({ type: 'add', line: b[j++] });
  return ops;
}
