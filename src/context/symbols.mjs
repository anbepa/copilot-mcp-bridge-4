/**
 * Mapa de símbolos por expresiones regulares (sin tree-sitter, cero dependencias).
 * No es un parser: es un índice "suficientemente bueno" para que el modelo sepa
 * qué hay en cada archivo sin tener que leerlo entero. Ese es todo su trabajo.
 */

const PATTERNS = {
  js: [
    { rx: /^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z0-9_$]+)/gm, kind: 'fn' },
    { rx: /^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)/gm, kind: 'class' },
    { rx: /^\s*(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>/gm, kind: 'fn' },
    { rx: /^\s*export\s+(?:interface|type)\s+([A-Za-z0-9_$]+)/gm, kind: 'type' }
  ],
  py: [
    { rx: /^\s*def\s+([A-Za-z0-9_]+)/gm, kind: 'fn' },
    { rx: /^\s*async\s+def\s+([A-Za-z0-9_]+)/gm, kind: 'fn' },
    { rx: /^\s*class\s+([A-Za-z0-9_]+)/gm, kind: 'class' }
  ],
  java: [
    { rx: /^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?class\s+([A-Za-z0-9_]+)/gm, kind: 'class' },
    { rx: /^\s*(?:public|private|protected)\s+(?:static\s+)?[A-Za-z0-9_<>[\],\s]+\s+([A-Za-z0-9_]+)\s*\(/gm, kind: 'method' }
  ],
  go: [
    { rx: /^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z0-9_]+)/gm, kind: 'fn' },
    { rx: /^\s*type\s+([A-Za-z0-9_]+)\s+struct/gm, kind: 'type' }
  ],
  rs: [
    { rx: /^\s*(?:pub\s+)?fn\s+([A-Za-z0-9_]+)/gm, kind: 'fn' },
    { rx: /^\s*(?:pub\s+)?struct\s+([A-Za-z0-9_]+)/gm, kind: 'type' }
  ]
};

const EXT_LANG = {
  '.js': 'js', '.mjs': 'js', '.cjs': 'js', '.jsx': 'js', '.ts': 'js', '.tsx': 'js',
  '.py': 'py', '.java': 'java', '.go': 'go', '.rs': 'rs'
};

export function langForExt(ext) {
  return EXT_LANG[ext] ?? null;
}

/** @returns {{symbols:string[], todos:{line:number,text:string}[], lines:number}} */
export function extractSymbols(content, ext) {
  const lang = langForExt(ext);
  const symbols = [];
  if (lang && PATTERNS[lang]) {
    for (const { rx, kind } of PATTERNS[lang]) {
      rx.lastIndex = 0;
      let m;
      while ((m = rx.exec(content)) !== null) {
        const name = m[1];
        if (!name || name === 'if' || name === 'for' || name === 'while' || name === 'switch' || name === 'catch') continue;
        const tag = `${kind}:${name}`;
        if (!symbols.includes(tag)) symbols.push(tag);
        if (symbols.length > 60) break;
      }
    }
  }

  const todos = [];
  const lines = content.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (/\b(TODO|FIXME|HACK|XXX)\b/.test(lines[i])) {
      todos.push({ line: i + 1, text: lines[i].trim().slice(0, 160) });
      if (todos.length > 50) break;
    }
  }

  return { symbols, todos, lines: lines.length };
}
