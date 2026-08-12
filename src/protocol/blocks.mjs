/**
 * Parser de bloques del protocolo.
 *
 * Copilot responde en markdown. Extraemos vallas de código etiquetadas:
 *   ```mcp-plan   { steps: [...] }
 *   ```mcp-action { id, server, tool, args }
 *   ```mcp-done   { summary }
 *   ```mcp-ask    { question }
 *
 * Es la pieza más frágil del sistema: el modelo a veces añade prosa, usa
 * ```json, comillas tipográficas o comas colgantes. Aquí se repara todo eso.
 */

const KINDS = ['mcp-plan', 'mcp-action', 'mcp-done', 'mcp-ask'];

/** Extrae todas las vallas de código de un texto markdown. Soporta ``` y ~~~ y anidamiento por longitud. */
export function extractFences(text) {
  const fences = [];
  const re = /^([ \t]*)(`{3,}|~{3,})[ \t]*([A-Za-z0-9_-]*)[ \t]*\r?\n([\s\S]*?)^[ \t]*\2[ \t]*$/gm;
  let m;
  while ((m = re.exec(text)) !== null) {
    fences.push({ lang: (m[3] || '').toLowerCase(), body: m[4], index: m.index });
  }
  return fences;
}

/** Repara desviaciones típicas de un LLM para que JSON.parse funcione. */
export function repairJson(raw) {
  let s = raw.trim();

  // Comillas tipográficas → rectas (fuera de posibles cadenas ya válidas es seguro aquí)
  s = s.replace(/[\u201C\u201D\u201E\u201F]/g, '"').replace(/[\u2018\u2019\u201A\u201B]/g, "'");
  // Espacios no separables
  s = s.replace(/\u00A0/g, ' ');
  // Comentarios de línea // y de bloque /* */ (el modelo los cuela a veces)
  s = s.replace(/^\s*\/\/.*$/gm, '');
  s = s.replace(/\/\*[\s\S]*?\*\//g, '');
  // Comas colgantes antes de } o ]
  s = s.replace(/,(\s*[}\]])/g, '$1');
  // Recorta a la primera { o [ y su cierre equilibrado
  const start = s.search(/[{[]/);
  if (start > 0) s = s.slice(start);
  return s.trim();
}

/** Intenta parsear JSON con reparación progresiva. */
export function parseLoose(raw) {
  const attempts = [raw, repairJson(raw)];
  for (const a of attempts) {
    try {
      return { ok: true, value: JSON.parse(a) };
    } catch {}
  }
  // Último recurso: recortar al bloque equilibrado desde el primer {
  const s = repairJson(raw);
  const open = s.indexOf('{');
  if (open >= 0) {
    let depth = 0, inStr = false, esc = false;
    for (let i = open; i < s.length; i++) {
      const ch = s[i];
      if (esc) { esc = false; continue; }
      if (ch === '\\') { esc = true; continue; }
      if (ch === '"') { inStr = !inStr; continue; }
      if (inStr) continue;
      if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          try {
            return { ok: true, value: JSON.parse(s.slice(open, i + 1)) };
          } catch (e) {
            return { ok: false, error: e.message };
          }
        }
      }
    }
  }
  return { ok: false, error: 'No se encontró JSON válido' };
}

/**
 * Analiza una respuesta completa de Copilot.
 * @returns {{kind:'plan'|'action'|'done'|'ask'|'none', value?:object, error?:string, prose:string}}
 */
export function parseReply(text) {
  if (!text || !text.trim()) return { kind: 'none', error: 'Respuesta vacía', prose: '' };

  const fences = extractFences(text);
  const prose = text.replace(/^([ \t]*)(`{3,}|~{3,})[\s\S]*?^[ \t]*\2[ \t]*$/gm, '').trim();

  // 1) Valla etiquetada explícitamente (camino feliz)
  for (const kind of KINDS) {
    const f = fences.find((x) => x.lang === kind);
    if (f) {
      const p = parseLoose(f.body);
      if (p.ok) return { kind: kind.replace('mcp-', ''), value: p.value, prose };
      return { kind: kind.replace('mcp-', ''), error: `JSON inválido en \`\`\`${kind}: ${p.error}`, prose };
    }
  }

  // 2) Valla json/sin etiqueta: inferimos por forma del objeto
  for (const f of fences) {
    if (f.lang && !['json', 'javascript', 'js', ''].includes(f.lang)) continue;
    const p = parseLoose(f.body);
    if (!p.ok) continue;
    const inferred = inferKind(p.value);
    if (inferred) return { kind: inferred, value: p.value, prose, inferred: true };
  }

  // 3) JSON desnudo sin valla
  const bare = parseLoose(text);
  if (bare.ok) {
    const inferred = inferKind(bare.value);
    if (inferred) return { kind: inferred, value: bare.value, prose, inferred: true };
  }

  return { kind: 'none', error: 'No se encontró ningún bloque mcp-* válido', prose };
}

function inferKind(v) {
  if (!v || typeof v !== 'object') return null;
  if (Array.isArray(v.steps)) return 'plan';
  if (typeof v.summary === 'string') return 'done';
  if (typeof v.question === 'string') return 'ask';
  if (v.tool && (v.server || v.args)) return 'action';
  return null;
}

/** Serializa un bloque para inyectarlo de vuelta en el chat. */
export function renderBlock(kind, value) {
  return '```mcp-' + kind + '\n' + JSON.stringify(value, null, 2) + '\n```';
}
