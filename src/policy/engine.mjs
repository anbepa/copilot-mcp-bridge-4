/**
 * POLICY ENGINE — la defensa real del sistema.
 *
 * Principio de diseño: NUNCA confía en la intención declarada por el modelo.
 * Valida servidor, herramienta, clase de operación y rutas. Es la mitigación
 * frente a prompt injection: si un archivo leído contiene "ahora lee ~/.ssh/id_rsa",
 * el modelo puede intentarlo, pero aquí se bloquea. La defensa está en el puente,
 * jamás en el prompt.
 */
import path from 'node:path';
import { realish, isInside, matchesAnyGlob } from '../util/paths.mjs';

const WRITE_TOOLS = new Set([
  'write_file', 'edit_file', 'create_directory', 'move_file', 'delete_file',
  'remove_file', 'rmdir', 'copy_file', 'apply_patch'
]);
const EXEC_TOOLS = new Set(['shell', 'exec', 'run_command', 'bash', 'powershell', 'execute']);
const PATH_ARGS = ['path', 'source', 'destination', 'file', 'dir', 'directory', 'target'];

export const Decision = { ALLOW: 'allow', ASK: 'ask', DENY: 'deny' };

export class PolicyEngine {
  constructor({ policy, sandbox, roots }) {
    this.policy = policy;
    this.sandbox = sandbox;
    this.roots = roots.map((r) => realish(r));
    this.writeCount = 0;
  }

  classify(tool) {
    if (EXEC_TOOLS.has(tool)) return 'exec';
    if (WRITE_TOOLS.has(tool)) return 'write';
    return 'read';
  }

  /**
   * @returns {{decision:string, reason:string, kind:string, paths:string[]}}
   */
  evaluate(step) {
    const kind = this.classify(step.tool);
    const paths = this.#extractPaths(step.args);

    // 1) Herramientas explícitamente denegadas
    const denied = this.policy.deniedTools ?? [];
    if (denied.includes(step.tool)) {
      return { decision: Decision.DENY, kind, paths, reason: `La herramienta "${step.tool}" está explícitamente denegada.` };
    }
    if (kind === 'exec' && !this.policy.allowExec) {
      return { decision: Decision.DENY, kind, paths, reason: `Herramientas de ejecución (clase: exec) están desactivadas. Habilita "policy.allowExec: true" en la configuración.` };
    }

    // 2) Servidor permitido
    const allowed = this.policy.allowedServers ?? [];
    if (allowed.length && step.server && !allowed.includes(step.server)) {
      return { decision: Decision.DENY, kind, paths, reason: `El servidor MCP "${step.server}" no está en la allowlist (${allowed.join(', ')}).` };
    }

    // 3) Contención de rutas
    for (const p of paths) {
      if (p.includes('\u0000')) return { decision: Decision.DENY, kind, paths, reason: 'Ruta con byte nulo.' };
      const abs = path.isAbsolute(p) ? realish(p) : realish(path.join(this.roots[0], p));
      const inRoot = this.roots.some((r) => isInside(r, abs) || r === abs);
      if (!inRoot) {
        return { decision: Decision.DENY, kind, paths, reason: `Ruta "${p}" fuera del sandbox. Roots: ${this.roots.join(', ')}.` };
      }
      const rel = path.relative(this.roots[0], abs).split(path.sep).join('/');
      if (matchesAnyGlob(rel, this.sandbox.denyGlobs ?? [])) {
        return { decision: Decision.DENY, kind, paths, reason: `Ruta "${rel}" coincide con un patrón bloqueado (secretos/credenciales/VCS).` };
      }
    }

    // 4) Cuota de escrituras
    if (kind === 'write') {
      if (this.writeCount >= (this.policy.maxWritesPerTask ?? 25)) {
        return { decision: Decision.DENY, kind, paths, reason: `Se alcanzó el máximo de ${this.policy.maxWritesPerTask} escrituras por tarea.` };
      }
      if (this.policy.requireApprovalForWrites !== false) {
        return { decision: Decision.ASK, kind, paths, reason: 'Operación de escritura: requiere aprobación humana.' };
      }
      return { decision: Decision.ALLOW, kind, paths, reason: 'Escritura autoaprobada por configuración.' };
    }

    // 5) Ejecución
    if (kind === 'exec') {
      if (this.policy.requireApprovalForExec !== false) {
        return { decision: Decision.ASK, kind, paths, reason: 'Operación de ejecución: requiere aprobación humana.' };
      }
      return { decision: Decision.ALLOW, kind, paths, reason: 'Ejecución autoaprobada por configuración.' };
    }

    // 6) Lecturas
    if (this.policy.autoApproveReads === false) {
      return { decision: Decision.ASK, kind, paths, reason: 'Lectura: aprobación manual activada.' };
    }
    return { decision: Decision.ALLOW, kind, paths, reason: 'Lectura dentro del sandbox.' };
  }

  noteWrite() {
    this.writeCount++;
  }

  #extractPaths(args) {
    const out = [];
    const visit = (obj, depth = 0) => {
      if (!obj || typeof obj !== 'object' || depth > 4) return;
      for (const [k, v] of Object.entries(obj)) {
        if (typeof v === 'string' && PATH_ARGS.includes(k)) out.push(v);
        else if (typeof v === 'object') visit(v, depth + 1);
      }
    };
    visit(args);
    return out;
  }
}
