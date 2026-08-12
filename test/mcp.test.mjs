import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { McpHost } from '../src/mcp/host.mjs';

const PROJECT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let tmpRoot;
let host;

before(async () => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'cmb-test-'));
  fs.mkdirSync(path.join(tmpRoot, 'src'), { recursive: true });
  fs.writeFileSync(path.join(tmpRoot, 'src', 'a.js'), '// TODO: arreglar\nexport function foo() { return 1; }\n');
  fs.writeFileSync(path.join(tmpRoot, 'src', 'b.js'), 'export const bar = () => 2;\n');
  fs.writeFileSync(path.join(tmpRoot, 'README.md'), '# Test\n');

  host = new McpHost({
    servers: { fs: { command: 'node', args: ['./src/mcp/servers/fs-server.mjs', '{{ROOTS}}'] } },
    roots: [tmpRoot],
    cwd: PROJECT
  });
  await host.start();
});

after(async () => {
  await host?.stop();
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

test('handshake e inventario de herramientas', () => {
  const cat = host.catalog();
  assert.ok(cat.length >= 10, `esperaba >=10 herramientas, hay ${cat.length}`);
  assert.ok(cat.some((t) => t.name === 'read_text_file'));
  assert.ok(cat.some((t) => t.name === 'edit_file'));
});

test('read_text_file devuelve contenido', async () => {
  const r = await host.callTool('fs', 'read_text_file', { path: 'src/a.js' });
  assert.equal(r.ok, true);
  assert.match(r.data, /TODO: arreglar/);
});

test('read_text_file con offset/limit', async () => {
  const r = await host.callTool('fs', 'read_text_file', { path: 'src/a.js', offset: 2, limit: 1 });
  assert.equal(r.ok, true);
  assert.match(r.data, /export function foo/);
  assert.ok(!r.data.includes('TODO'));
});

test('grep localiza coincidencias con ruta y línea', async () => {
  const r = await host.callTool('fs', 'grep', { path: '.', pattern: 'TODO' });
  assert.equal(r.ok, true);
  assert.match(r.data, /src\/a\.js:1/);
});

test('directory_tree omite ruido', async () => {
  const r = await host.callTool('fs', 'directory_tree', { path: '.' });
  assert.equal(r.ok, true);
  assert.match(r.data, /a\.js/);
});

test('el sandbox bloquea traversal en el propio servidor MCP', async () => {
  const r = await host.callTool('fs', 'read_text_file', { path: '../../../etc/passwd' });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, 'EACCES');
});

test('edit_file con dryRun no toca el disco', async () => {
  const before = fs.readFileSync(path.join(tmpRoot, 'src', 'a.js'), 'utf8');
  const r = await host.callTool('fs', 'edit_file', {
    path: 'src/a.js',
    edits: [{ oldText: '// TODO: arreglar', newText: '// Arreglado' }],
    dryRun: true
  });
  assert.equal(r.ok, true);
  assert.equal(fs.readFileSync(path.join(tmpRoot, 'src', 'a.js'), 'utf8'), before);
});

test('edit_file aplica cambios reales', async () => {
  const r = await host.callTool('fs', 'edit_file', {
    path: 'src/b.js',
    edits: [{ oldText: 'bar = () => 2', newText: 'bar = () => 42' }]
  });
  assert.equal(r.ok, true);
  assert.match(fs.readFileSync(path.join(tmpRoot, 'src', 'b.js'), 'utf8'), /=> 42/);
});

test('edit_file falla con ancla inexistente y da pista accionable', async () => {
  const r = await host.callTool('fs', 'edit_file', {
    path: 'src/b.js',
    edits: [{ oldText: 'NO_EXISTE_ESTE_TEXTO', newText: 'x' }]
  });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, 'ENOMATCH');
});

test('edit_file rechaza anclas ambiguas', async () => {
  fs.writeFileSync(path.join(tmpRoot, 'src', 'dup.js'), 'let x = 1;\nlet x = 1;\n');
  const r = await host.callTool('fs', 'edit_file', {
    path: 'src/dup.js',
    edits: [{ oldText: 'let x = 1;', newText: 'let y = 2;' }]
  });
  assert.equal(r.ok, false);
  assert.equal(r.error.code, 'EAMBIGUOUS');
});

test('write_file crea directorios intermedios', async () => {
  const r = await host.callTool('fs', 'write_file', { path: 'nuevo/dir/x.txt', content: 'hola' });
  assert.equal(r.ok, true);
  assert.equal(fs.readFileSync(path.join(tmpRoot, 'nuevo', 'dir', 'x.txt'), 'utf8'), 'hola');
});

test('herramienta inexistente devuelve error claro', async () => {
  const r = await host.callTool('fs', 'no_existe', {});
  assert.equal(r.ok, false);
  assert.match(r.error.message, /no existe/i);
});
