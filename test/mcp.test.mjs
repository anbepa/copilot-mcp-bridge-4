import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { McpHost } from '../src/mcp/host.mjs';

const PROJECT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STDIO_SERVER = './mcp-unified-server 3/stdio_server.py';

// El server unificado corre en Python. Detectamos el intérprete disponible.
function detectPython() {
  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return cmd;
    } catch {}
  }
  return null;
}

const PYTHON = detectPython();
// Si no hay Python, saltamos toda la suite (el server unificado no puede arrancar).
const t = PYTHON ? test : test.skip;

let tmpRoot;
let host;

before(async () => {
  if (!PYTHON) return;
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cmb-test-'));
  fs.mkdirSync(path.join(tmpRoot, 'src'), { recursive: true });
  fs.writeFileSync(path.join(tmpRoot, 'src', 'a.js'), '// TODO: arreglar\nexport function foo() { return 1; }\n');
  fs.writeFileSync(path.join(tmpRoot, 'src', 'b.js'), 'export const bar = () => 2;\n');
  fs.writeFileSync(path.join(tmpRoot, 'README.md'), '# Test\n');

  host = new McpHost({
    servers: {
      unified: { command: PYTHON, args: [STDIO_SERVER, '{{ROOTS}}'] }
    },
    roots: [tmpRoot],
    cwd: PROJECT
  });
  await host.start();
});

after(async () => {
  await host?.stop();
  if (tmpRoot) fs.rmSync(tmpRoot, { recursive: true, force: true });
});

t('handshake e inventario de herramientas (82 tools)', () => {
  const cat = host.catalog();
  assert.ok(cat.length >= 80, `esperaba >=80 herramientas, hay ${cat.length}`);
  assert.ok(cat.some((tool) => tool.name === 'read_file'));
  assert.ok(cat.some((tool) => tool.name === 'write_file'));
  assert.ok(cat.some((tool) => tool.name === 'search_nodes'));
  assert.ok(cat.some((tool) => tool.name.startsWith('browser_')));
});

t('read_file devuelve contenido', async () => {
  const r = await host.callTool('unified', 'read_file', { path: 'src/a.js' });
  assert.equal(r.ok, true);
  assert.match(r.data, /TODO: arreglar/);
});

t('search_nodes localiza por nombre', async () => {
  const r = await host.callTool('unified', 'search_nodes', { path: '.', query: '*.js' });
  assert.equal(r.ok, true);
  assert.match(r.data, /a\.js/);
});

t('search_nodes con search_content encuentra dentro de archivos', async () => {
  const r = await host.callTool('unified', 'search_nodes', {
    path: '.',
    query: 'TODO',
    search_content: true
  });
  assert.equal(r.ok, true);
  assert.match(r.data, /a\.js/);
});

t('list_directory lista el contenido', async () => {
  const r = await host.callTool('unified', 'list_directory', { path: 'src' });
  assert.equal(r.ok, true);
  assert.match(r.data, /a\.js/);
  assert.match(r.data, /b\.js/);
});

t('el sandbox bloquea traversal en el propio servidor MCP', async () => {
  const r = await host.callTool('unified', 'read_file', { path: '../../../etc/passwd' });
  assert.equal(r.ok, false);
});

t('write_file reescribe el archivo completo', async () => {
  const r = await host.callTool('unified', 'write_file', {
    path: 'src/b.js',
    content: 'export const bar = () => 42;\n'
  });
  assert.equal(r.ok, true);
  assert.match(fs.readFileSync(path.join(tmpRoot, 'src', 'b.js'), 'utf8'), /=> 42/);
});

t('write_file crea directorios intermedios', async () => {
  const r = await host.callTool('unified', 'write_file', { path: 'nuevo/dir/x.txt', content: 'hola' });
  assert.equal(r.ok, true);
  assert.equal(fs.readFileSync(path.join(tmpRoot, 'nuevo', 'dir', 'x.txt'), 'utf8'), 'hola');
});

t('edit_file con dryRun no toca el disco pero devuelve diff', async () => {
  const before = fs.readFileSync(path.join(tmpRoot, 'src', 'a.js'), 'utf8');
  const r = await host.callTool('unified', 'edit_file', {
    path: 'src/a.js',
    edits: [{ oldText: '// TODO: arreglar', newText: '// Arreglado' }],
    dryRun: true
  });
  assert.equal(r.ok, true);
  assert.match(r.data, /Arreglado/);
  assert.equal(fs.readFileSync(path.join(tmpRoot, 'src', 'a.js'), 'utf8'), before);
});

t('edit_file aplica cambios reales por ancla', async () => {
  fs.writeFileSync(path.join(tmpRoot, 'src', 'c.js'), 'export const bar = () => 2;\n');
  const r = await host.callTool('unified', 'edit_file', {
    path: 'src/c.js',
    edits: [{ oldText: 'bar = () => 2', newText: 'bar = () => 42' }]
  });
  assert.equal(r.ok, true);
  assert.match(fs.readFileSync(path.join(tmpRoot, 'src', 'c.js'), 'utf8'), /=> 42/);
});

t('edit_file falla con ancla inexistente y da pista accionable', async () => {
  const r = await host.callTool('unified', 'edit_file', {
    path: 'src/a.js',
    edits: [{ oldText: 'NO_EXISTE_ESTE_TEXTO', newText: 'x' }]
  });
  assert.equal(r.ok, false);
  assert.match(r.error.message, /ENOMATCH/);
});

t('edit_file rechaza anclas ambiguas', async () => {
  fs.writeFileSync(path.join(tmpRoot, 'src', 'dup.js'), 'let x = 1;\nlet x = 1;\n');
  const r = await host.callTool('unified', 'edit_file', {
    path: 'src/dup.js',
    edits: [{ oldText: 'let x = 1;', newText: 'let y = 2;' }]
  });
  assert.equal(r.ok, false);
  assert.match(r.error.message, /EAMBIGUOUS/);
});

t('herramienta inexistente devuelve error claro', async () => {
  const r = await host.callTool('unified', 'no_existe', {});
  assert.equal(r.ok, false);
  assert.match(r.error.message, /no existe/i);
});
