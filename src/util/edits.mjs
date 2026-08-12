/**
 * Normalización y aplicación de ediciones por ancla de texto.
 *
 * Lección aprendida en producción: los LLM NO respetan los nombres de campo del
 * esquema. Copilot emite {search, replace}; Claude {old_string, new_string};
 * otros {find, replaceWith}. Rechazarlos con "ENOMATCH: vuelve a leer el archivo"
 * es un error de diagnóstico que provoca bucles infinitos: el modelo relee un
 * archivo que ya tenía bien y vuelve a fallar por la misma razón.
 *
 * Reglas de este módulo:
 *   1. Aceptar cualquier alias razonable (Postel: sé liberal en lo que recibes).
 *   2. Si faltan los campos, decir EXACTAMENTE qué llegó y qué se esperaba.
 *   3. Si el ancla no coincide por espacios/indentación, reintentar con
 *      tolerancia antes de rendirse.
 *   4. Nunca dar un hint que mande a repetir lo que ya se hizo.
 */

const OLD_KEYS = ['oldText', 'old_text', 'oldString', 'old_string', 'oldStr', 'old', 'search', 'searchText', 'find', 'from', 'before', 'target', 'pattern'];
const NEW_KEYS = ['newText', 'new_text', 'newString', 'new_string', 'newStr', 'new', 'replace', 'replaceText', 'replaceWith', 'replacement', 'to', 'after', 'content'];

function pick(obj, keys) {
  for (const k of keys) {
    if (typeof obj?.[k] === 'string') return obj[k];
  }
  return undefined;
}

/** Normaliza una edición a {oldText,newText}. Devuelve {ok,edit,error}. */
export function normalizeEdit(ed, index = 0) {
  const n = index + 1;
  if (!ed || typeof ed !== 'object' || Array.isArray(ed)) {
    return { ok: false, error: { code: 'EBADEDIT', message: `La edición #${n} no es un objeto.`, hint: 'Cada elemento de "edits" debe ser {"oldText": "...", "newText": "..."}.' } };
  }
  const oldText = pick(ed, OLD_KEYS);
  const newText = pick(ed, NEW_KEYS);

  if (typeof oldText !== 'string' || typeof newText !== 'string') {
    const got = Object.keys(ed);
    const missing = [];
    if (typeof oldText !== 'string') missing.push('oldText');
    if (typeof newText !== 'string') missing.push('newText');
    return {
      ok: false,
      error: {
        code: 'EBADEDIT',
        message:
          `La edición #${n} no tiene los campos requeridos: falta ${missing.join(' y ')}. ` +
          `Campos recibidos: [${got.join(', ')}].`,
        // Hint accionable y específico: el problema es el ESQUEMA, no el contenido.
        hint:
          'NO vuelvas a leer el archivo: el contenido no es el problema. Cambia los nombres de los campos. ' +
          'El formato correcto es exactamente: {"oldText": "texto a buscar", "newText": "texto nuevo"}.'
      }
    };
  }
  if (oldText.length === 0) {
    return { ok: false, error: { code: 'EBADEDIT', message: `La edición #${n} tiene "oldText" vacío.`, hint: 'Para crear un archivo nuevo usa write_file, no edit_file.' } };
  }
  return { ok: true, edit: { oldText, newText } };
}

export function normalizeEdits(edits) {
  if (!Array.isArray(edits) || edits.length === 0) {
    return { ok: false, error: { code: 'EBADEDIT', message: '"edits" debe ser un array con al menos una edición.', hint: 'Formato: "edits": [{"oldText": "...", "newText": "..."}]' } };
  }
  const out = [];
  for (const [i, ed] of edits.entries()) {
    const r = normalizeEdit(ed, i);
    if (!r.ok) return r;
    out.push(r.edit);
  }
  return { ok: true, edits: out };
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Busca `needle` en `hay` tolerando diferencias de espacio en blanco.
 * Devuelve {start,end,count} sobre el texto ORIGINAL, o null.
 */
export function flexibleFind(hay, needle) {
  const pattern = needle
    .split(/\r?\n/)
    .map((line) => escapeRe(line.trim()))
    .join('[ \\t]*\\r?\\n[ \\t]*');
  let re;
  try {
    re = new RegExp('[ \\t]*' + pattern, 'g');
  } catch {
    return null;
  }
  const matches = [...hay.matchAll(re)];
  if (matches.length === 0) return null;
  const m = matches[0];
  return { start: m.index, end: m.index + m[0].length, count: matches.length };
}

/** Pista de diagnóstico: ¿existe una línea parecida? Ayuda al modelo a corregir. */
function nearestHint(content, oldText) {
  const first = oldText.split(/\r?\n/)[0].trim();
  if (first.length < 4) return null;
  const key = first.slice(0, Math.min(28, first.length));
  const lines = content.split(/\r?\n/);
  for (const [i, line] of lines.entries()) {
    if (line.includes(key) || (key.length > 8 && line.trim().startsWith(key.slice(0, 12)))) {
      return `La línea ${i + 1} del archivo es: ${JSON.stringify(line)}. Cópiala EXACTAMENTE, con su indentación.`;
    }
  }
  return null;
}

/**
 * Aplica ediciones ya normalizadas. Devuelve {ok, updated, applied, fuzzy} o {ok:false,error}.
 */
export function applyEdits(content, edits) {
  // Defensivo: si el llamador no normalizó, normalizamos aquí. Un fallo de
  // higiene interna no debe convertirse en un TypeError opaco para el usuario.
  const norm = normalizeEdits(edits);
  if (!norm.ok) return { ok: false, error: norm.error };

  let updated = content;
  const applied = [];
  let fuzzy = 0;

  for (const [i, ed] of norm.edits.entries()) {
    const n = i + 1;
    const occurrences = updated.split(ed.oldText).length - 1;

    if (occurrences === 1) {
      updated = updated.replace(ed.oldText, () => ed.newText);
      applied.push(n);
      continue;
    }
    if (occurrences > 1) {
      return {
        ok: false,
        error: {
          code: 'EAMBIGUOUS',
          message: `La edición #${n} coincide ${occurrences} veces. Debe ser única.`,
          hint: 'Amplía el ancla con la línea anterior o posterior para hacerla inequívoca.'
        }
      };
    }

    // Sin coincidencia exacta → reintento tolerante a espacios.
    const found = flexibleFind(updated, ed.oldText);
    if (found && found.count === 1) {
      updated = updated.slice(0, found.start) + ed.newText + updated.slice(found.end);
      applied.push(n);
      fuzzy++;
      continue;
    }
    if (found && found.count > 1) {
      return {
        ok: false,
        error: {
          code: 'EAMBIGUOUS',
          message: `La edición #${n} coincide ${found.count} veces ignorando espacios. Debe ser única.`,
          hint: 'Amplía el ancla con más contexto para hacerla inequívoca.'
        }
      };
    }

    return {
      ok: false,
      error: {
        code: 'ENOMATCH',
        message: `La edición #${n} no encontró su texto ancla.`,
        hint: nearestHint(updated, ed.oldText) ?? 'Lee el archivo con read_text_file y copia un fragmento textual, con su indentación exacta.'
      }
    };
  }
  return { ok: true, updated, applied, fuzzy };
}
