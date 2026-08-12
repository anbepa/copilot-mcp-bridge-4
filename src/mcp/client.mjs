/**
 * Cliente MCP mínimo — JSON-RPC 2.0 sobre stdio, delimitado por saltos de línea.
 * Cero dependencias. Compatible con cualquier servidor MCP estándar
 * (incluido @modelcontextprotocol/server-filesystem vía npx).
 */
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { log } from '../log.mjs';

const PROTOCOL_VERSION = '2024-11-05';

export class McpClient extends EventEmitter {
  /**
   * @param {{name:string, command:string, args:string[], cwd?:string, env?:object}} opts
   */
  constructor(opts) {
    super();
    this.name = opts.name;
    this.command = opts.command;
    this.args = opts.args ?? [];
    this.cwd = opts.cwd ?? process.cwd();
    this.env = { ...process.env, ...(opts.env ?? {}) };

    this.proc = null;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    this.tools = [];
    this.serverInfo = null;
    this.closed = false;
  }

  async start(timeoutMs = 20000) {
    this.proc = spawn(this.command, this.args, {
      cwd: this.cwd,
      env: this.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      shell: process.platform === 'win32' // npx/cmd en Windows lo requiere
    });

    this.proc.on('error', (err) => {
      this._failAll(new Error(`No se pudo lanzar el servidor MCP "${this.name}": ${err.message}`));
    });
    this.proc.stdout.setEncoding('utf8');
    this.proc.stdout.on('data', (chunk) => this._onData(chunk));
    this.proc.stderr.setEncoding('utf8');
    this.proc.stderr.on('data', (d) => log.debug(`[mcp:${this.name}:stderr]`, d.trim()));
    this.proc.on('exit', (code) => {
      this.closed = true;
      this._failAll(new Error(`Servidor MCP "${this.name}" terminó con código ${code}`));
      this.emit('exit', code);
    });

    const init = await this.request(
      'initialize',
      {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: { roots: { listChanged: false }, sampling: {} },
        clientInfo: { name: 'copilot-mcp-bridge', version: '0.1.0' }
      },
      timeoutMs
    );
    this.serverInfo = init?.serverInfo ?? null;

    // La notificación de "initialized" es obligatoria por spec.
    this.notify('notifications/initialized', {});

    await this.refreshTools(timeoutMs);
    return this;
  }

  async refreshTools(timeoutMs = 20000) {
    const res = await this.request('tools/list', {}, timeoutMs);
    this.tools = res?.tools ?? [];
    return this.tools;
  }

  hasTool(name) {
    return this.tools.some((t) => t.name === name);
  }

  /** Llama a una herramienta. Devuelve {ok, data, isError, raw}. */
  async callTool(name, args, timeoutMs = 60000) {
    try {
      const res = await this.request('tools/call', { name, arguments: args ?? {} }, timeoutMs);
      const text = (res?.content ?? [])
        .filter((c) => c.type === 'text')
        .map((c) => c.text)
        .join('\n');
      return { ok: !res?.isError, data: text, isError: !!res?.isError, raw: res };
    } catch (err) {
      return { ok: false, data: null, isError: true, error: err.message };
    }
  }

  request(method, params, timeoutMs = 30000) {
    if (this.closed) return Promise.reject(new Error(`Servidor MCP "${this.name}" cerrado`));
    const id = this.nextId++;
    const payload = { jsonrpc: '2.0', id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timeout (${timeoutMs}ms) en ${method} sobre "${this.name}"`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this._write(payload);
    });
  }

  notify(method, params) {
    this._write({ jsonrpc: '2.0', method, params });
  }

  _write(obj) {
    if (!this.proc?.stdin.writable) return;
    this.proc.stdin.write(JSON.stringify(obj) + '\n');
  }

  _onData(chunk) {
    this.buffer += chunk;
    let idx;
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        log.debug(`[mcp:${this.name}] línea no-JSON ignorada:`, line.slice(0, 120));
        continue;
      }
      this._dispatch(msg);
    }
  }

  _dispatch(msg) {
    if (msg.id != null && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      clearTimeout(timer);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.code}: ${msg.error.message}`));
      else resolve(msg.result);
      return;
    }
    if (msg.method) this.emit('notification', msg);
  }

  _failAll(err) {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  async stop() {
    this.closed = true;
    try {
      this.proc?.stdin.end();
    } catch {}
    if (this.proc && this.proc.exitCode == null) {
      const pid = this.proc.pid;
      await new Promise((r) => setTimeout(r, 150));
      if (this.proc.exitCode == null && pid) {
        try {
          process.kill(pid, 'SIGTERM');
        } catch {}
      }
    }
  }
}
