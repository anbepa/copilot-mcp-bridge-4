/**
 * CONTEXT COMPILER — la optimización de mayor impacto del sistema.
 *
 * En vez de dejar que Copilot descubra el proyecto con 10 turnos de llamadas
 * (read_file → read_file → …), reunimos todo localmente y se lo entregamos
 * en el turno 1: árbol podado + mapa de símbolos + archivos clave + búsquedas
 * ya resueltas + manifiesto con hashes para envíos incrementales.
 *
 * Descubrimiento: N turnos → 0.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { extractSymbols } from './symbols.mjs';
import { matchesAnyGlob, realish } from '../util/paths.mjs';
import { truncateHeadTail, byteLength } from '../util/truncate.mjs';

const SKIP_DIRS = new Set([
  '.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build',
  '.next', 'target', 'coverage', '.audit', '.browser-profile', '.idea', '.vscode'
]);

export class ContextCompiler {
  constructor({ root, config, denyGlobs = [] }) {
    this.root = realish(root);
    this.cfg = config;
    this.denyGlobs = denyGlobs;
  }

  /**
   * @param {{task?:string, previousManifest?:object}} opts
   * @returns {Promise<{pack:string, manifest:object, stats:object}>}
   */
  async compile({ task = '', previousManifest = null } = {}) {
    const files = await this.#scan();
    const manifest = {};
    const sections = [];
    const stats = { files: files.length, bytes: 0, symbols: 0, todos: 0, unchanged: 0 };

    // 1) Árbol podado
    sections.push('## ÁRBOL DEL PROYECTO\n```\n' + this.#renderTree(files) + '\n```');

    // 2) Archivos clave, completos (configs, README…)
    const keySet = new Set(this.cfg.keyFiles ?? []);
    const keyOut = [];
    for (const f of files) {
      if (!keySet.has(path.basename(f.rel))) continue;
      const content = await this.#read(f.abs);
      if (content == null) continue;
      const { text } = truncateHeadTail(content, this.cfg.maxKeyFileBytes ?? 6000);
      keyOut.push(`### ${f.rel}\n\`\`\`\n${text}\n\`\`\``);
    }
    if (keyOut.length) sections.push('## ARCHIVOS CLAVE\n' + keyOut.join('\n\n'));

    // 3) Mapa de símbolos + TODOs
    const codeExts = new Set(this.cfg.codeExtensions ?? []);
    const symbolLines = [];
    const todoLines = [];
    let processed = 0;
    for (const f of files) {
      if (processed >= (this.cfg.maxSymbolFiles ?? 120)) break;
      if (!codeExts.has(path.extname(f.rel))) continue;
      const content = await this.#read(f.abs);
      if (content == null) continue;
      processed++;

      const hash = crypto.createHash('sha256').update(content).digest('hex').slice(0, 12);
      manifest[f.rel] = { hash, size: f.size, lines: content.split('\n').length };

      if (previousManifest?.[f.rel]?.hash === hash) {
        stats.unchanged++;
        symbolLines.push(`${f.rel} (${manifest[f.rel].lines}L) — SIN CAMBIOS desde la última sesión`);
        continue;
      }

      const { symbols, todos } = extractSymbols(content, path.extname(f.rel));
      stats.symbols += symbols.length;
      stats.todos += todos.length;
      symbolLines.push(`${f.rel} (${manifest[f.rel].lines}L): ${symbols.length ? symbols.join(', ') : '—'}`);
      for (const t of todos) todoLines.push(`${f.rel}:${t.line}: ${t.text}`);
    }
    if (symbolLines.length) sections.push('## MAPA DE SÍMBOLOS\n```\n' + symbolLines.join('\n') + '\n```');
    if (todoLines.length) sections.push('## MARCADORES TODO/FIXME ENCONTRADOS\n```\n' + todoLines.join('\n') + '\n```');

    // 4) Búsquedas derivadas de la tarea, ya resueltas localmente
    const terms = this.#termsFromTask(task);
    if (terms.length) {
      const hits = await this.#grepTerms(files, terms);
      if (hits.length) {
        sections.push(
          `## BÚSQUEDAS PRERESUELTAS (términos: ${terms.join(', ')})\n\`\`\`\n` + hits.slice(0, 80).join('\n') + '\n```'
        );
      }
    }

    let pack = sections.join('\n\n');
    const max = this.cfg.maxPackBytes ?? 60000;
    if (byteLength(pack) > max) pack = truncateHeadTail(pack, max).text;
    stats.bytes = byteLength(pack);

    return { pack, manifest, stats };
  }

  async #scan() {
    const out = [];
    const walk = async (dir, depth) => {
      if (depth > 12 || out.length > 5000) return;
      let entries;
      try {
        entries = await fs.readdir(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const e of entries) {
        if (SKIP_DIRS.has(e.name)) continue;
        const abs = path.join(dir, e.name);
        const rel = path.relative(this.root, abs).split(path.sep).join('/');
        if (matchesAnyGlob(rel, this.denyGlobs)) continue;
        if (e.isDirectory()) await walk(abs, depth + 1);
        else if (e.isFile()) {
          const st = await fs.stat(abs).catch(() => null);
          if (st) out.push({ abs, rel, size: st.size });
        }
      }
    };
    await walk(this.root, 0);
    out.sort((a, b) => a.rel.localeCompare(b.rel));
    return out;
  }

  async #read(abs) {
    try {
      const buf = await fs.readFile(abs);
      if (buf.includes(0)) return null; // binario
      if (buf.length > 400 * 1024) return null;
      return buf.toString('utf8');
    } catch {
      return null;
    }
  }

  #renderTree(files) {
    const maxEntries = this.cfg.maxTreeEntries ?? 400;
    const tree = {};
    for (const f of files.slice(0, maxEntries)) {
      const parts = f.rel.split('/');
      let node = tree;
      for (let i = 0; i < parts.length; i++) {
        const isFile = i === parts.length - 1;
        node[parts[i]] ??= isFile ? null : {};
        if (!isFile) node = node[parts[i]];
      }
    }
    const lines = [];
    const render = (node, prefix) => {
      const keys = Object.keys(node).sort((a, b) => {
        const ad = node[a] !== null, bd = node[b] !== null;
        return ad === bd ? a.localeCompare(b) : ad ? -1 : 1;
      });
      keys.forEach((k, i) => {
        const last = i === keys.length - 1;
        lines.push(prefix + (last ? '└── ' : '├── ') + k + (node[k] !== null ? '/' : ''));
        if (node[k] !== null) render(node[k], prefix + (last ? '    ' : '│   '));
      });
    };
    render(tree, '');
    if (files.length > maxEntries) lines.push(`… y ${files.length - maxEntries} archivos más (truncado)`);
    return lines.join('\n');
  }

  #termsFromTask(task) {
    if (!task) return [];
    const stop = new Set([
      'el','la','los','las','de','del','en','y','o','que','para','con','por','un','una','se','su','al',
      'archivo','archivos','proyecto','codigo','código','favor','quiero','necesito','haz','hacer','crea',
      'the','a','an','of','in','to','and','or','for','with','file','files','please'
    ]);
    const words = (task.toLowerCase().match(/[a-záéíóúñ_][a-záéíóúñ0-9_]{3,}/gi) ?? [])
      .filter((w) => !stop.has(w));
    const upper = task.match(/\b(TODO|FIXME|HACK|XXX)\b/g) ?? [];
    return [...new Set([...upper, ...words])].slice(0, 5);
  }

  async #grepTerms(files, terms) {
    const rx = new RegExp(terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'i');
    const hits = [];
    const codeExts = new Set([...(this.cfg.codeExtensions ?? []), '.md', '.json', '.yml', '.yaml', '.txt']);
    for (const f of files) {
      if (!codeExts.has(path.extname(f.rel))) continue;
      const content = await this.#read(f.abs);
      if (content == null) continue;
      const lines = content.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (rx.test(lines[i])) {
          hits.push(`${f.rel}:${i + 1}: ${lines[i].trim().slice(0, 160)}`);
          if (hits.length >= 200) return hits;
        }
      }
    }
    return hits;
  }
}
