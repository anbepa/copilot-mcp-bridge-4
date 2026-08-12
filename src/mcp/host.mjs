/**
 * MCP Host: gestiona N clientes MCP y expone un catálogo unificado de herramientas
 * al orquestador (y, en forma resumida, a Copilot).
 */
import path from 'node:path';
import { McpClient } from './client.mjs';
import { log } from '../log.mjs';

export class McpHost {
  constructor({ servers, roots, cwd }) {
    this.serversConfig = servers;
    this.roots = roots;
    this.cwd = cwd ?? process.cwd();
    /** @type {Map<string, McpClient>} */
    this.clients = new Map();
  }

  async start() {
    for (const [name, cfg] of Object.entries(this.serversConfig)) {
      if (cfg.enabled === false) continue;
      const args = (cfg.args ?? []).flatMap((a) =>
        a === '{{ROOTS}}' ? this.roots : [a.replaceAll('{{CWD}}', this.cwd)]
      );
      const client = new McpClient({
        name,
        command: cfg.command,
        args,
        cwd: this.cwd,
        env: cfg.env
      });
      try {
        await client.start();
        this.clients.set(name, client);
        log.ok(`MCP "${name}" listo — ${client.tools.length} herramientas (${client.serverInfo?.name ?? '?'})`);
      } catch (err) {
        log.error(`MCP "${name}" no arrancó: ${err.message}`);
        await client.stop();
      }
    }
    if (this.clients.size === 0) throw new Error('Ningún servidor MCP disponible. Revisa la configuración.');
    return this;
  }

  /** Catálogo plano para el orquestador. */
  catalog() {
    const out = [];
    for (const [server, client] of this.clients) {
      for (const t of client.tools) {
        out.push({ server, name: t.name, description: t.description ?? '', inputSchema: t.inputSchema ?? {} });
      }
    }
    return out;
  }

  /** Versión compacta para inyectar en el prompt (ahorra tokens). */
  catalogForPrompt() {
    return this.catalog().map((t) => {
      const props = t.inputSchema?.properties ?? {};
      const required = new Set(t.inputSchema?.required ?? []);
      const params = Object.entries(props)
        .map(([k, v]) => `${k}${required.has(k) ? '' : '?'}: ${v.type ?? 'any'}`)
        .join(', ');
      return `- ${t.server}.${t.name}(${params}) — ${t.description}`;
    });
  }

  resolve(server, tool) {
    const client = this.clients.get(server);
    if (!client) return { error: `Servidor MCP desconocido: "${server}". Disponibles: ${[...this.clients.keys()].join(', ')}` };
    if (!client.hasTool(tool)) {
      const names = client.tools.map((t) => t.name).join(', ');
      return { error: `Herramienta "${tool}" no existe en "${server}". Disponibles: ${names}` };
    }
    return { client };
  }

  async callTool(server, tool, args, timeoutMs) {
    const r = this.resolve(server, tool);
    if (r.error) return { ok: false, error: { code: 'ENOTOOL', message: r.error } };
    const res = await r.client.callTool(tool, args, timeoutMs);
    if (!res.ok) {
      let parsed = null;
      try {
        parsed = JSON.parse(res.data);
      } catch {}
      return {
        ok: false,
        // El `hint` del servidor debe sobrevivir el transporte: es lo que permite
        // al modelo autocorregirse en vez de reintentar lo mismo hasta agotar turnos.
        error: parsed?.code
          ? { code: parsed.code, message: parsed.message, ...(parsed.hint ? { hint: parsed.hint } : {}) }
          : { code: 'ETOOL', message: res.error ?? res.data ?? 'fallo desconocido' }
      };
    }
    return { ok: true, data: res.data };
  }

  async stop() {
    for (const [, c] of this.clients) await c.stop();
    this.clients.clear();
  }
}
