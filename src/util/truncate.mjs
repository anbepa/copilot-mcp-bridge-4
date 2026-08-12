/**
 * Truncado con presupuesto de bytes. Nunca corta a mitad de un carácter multibyte
 * y siempre deja constancia explícita de que hubo recorte (para que el modelo lo sepa).
 */

export function byteLength(s) {
  return Buffer.byteLength(s, 'utf8');
}

/** Trunca `s` a `maxBytes`, marcando el recorte. Devuelve {text, truncated, originalBytes}. */
export function truncateBytes(s, maxBytes) {
  const original = byteLength(s);
  if (original <= maxBytes) return { text: s, truncated: false, originalBytes: original };

  const buf = Buffer.from(s, 'utf8');
  let cut = maxBytes;
  // Retrocede hasta un límite de carácter UTF-8 válido.
  while (cut > 0 && (buf[cut] & 0xc0) === 0x80) cut--;
  const head = buf.subarray(0, cut).toString('utf8');
  return {
    text: head + `\n\n[...TRUNCADO: ${original - cut} de ${original} bytes omitidos...]`,
    truncated: true,
    originalBytes: original
  };
}

/**
 * Truncado "inteligente" para texto con líneas: conserva cabeza y cola,
 * que es lo que suele importar en logs y archivos de código.
 */
export function truncateHeadTail(s, maxBytes, headRatio = 0.7) {
  if (byteLength(s) <= maxBytes) return { text: s, truncated: false };
  const lines = s.split('\n');
  const headBudget = Math.floor(maxBytes * headRatio);
  const tailBudget = maxBytes - headBudget;

  const head = [];
  let hb = 0;
  for (const l of lines) {
    const n = byteLength(l) + 1;
    if (hb + n > headBudget) break;
    head.push(l);
    hb += n;
  }
  const tail = [];
  let tb = 0;
  for (let i = lines.length - 1; i >= head.length; i--) {
    const n = byteLength(lines[i]) + 1;
    if (tb + n > tailBudget) break;
    tail.unshift(lines[i]);
    tb += n;
  }
  const omitted = lines.length - head.length - tail.length;
  if (omitted <= 0) return truncateBytes(s, maxBytes);
  return {
    text: head.join('\n') + `\n\n[...${omitted} líneas omitidas por presupuesto...]\n\n` + tail.join('\n'),
    truncated: true
  };
}

/** Recorta un objeto JSON serializado a presupuesto, degradando con elegancia. */
export function truncateJson(value, maxBytes) {
  const s = typeof value === 'string' ? value : JSON.stringify(value, null, 0);
  return truncateHeadTail(s, maxBytes);
}
