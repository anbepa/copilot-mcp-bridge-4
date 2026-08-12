/**
 * Ejecutor de planes: resuelve el DAG de dependencias y lanza cada oleada
 * EN PARALELO. Es lo que convierte N turnos de red en 1.
 */
import { toWaves } from '../protocol/validate.mjs';
import { Decision } from '../policy/engine.mjs';
import { truncateHeadTail, byteLength } from '../util/truncate.mjs';
import { log, color } from '../log.mjs';

export class PlanExecutor {
  constructor({ host, policy, budget, approver, audit, defaultServer }) {
    this.host = host;
    this.policy = policy;
    this.budget = budget;
    this.approver = approver;
    this.audit = audit;
    this.defaultServer = defaultServer;
  }

  /**
   * @param {Array} steps normalizados
   * @returns {Promise<{results:Array, wrote:boolean}>}
   */
  async execute(steps) {
    const waves = toWaves(steps);
    const results = [];
    const byId = new Map();
    let wrote = false;

    for (const [wi, wave] of waves.entries()) {
      log.step(`Oleada ${wi + 1}/${waves.length} — ${wave.length} paso(s) en paralelo`);

      // Las aprobaciones son secuenciales (interactivas); la ejecución, paralela.
      const approved = [];
      for (const step of wave) {
        step.server ??= this.defaultServer;
        const skip = step.depends_on.find((d) => byId.get(d)?.ok === false);
        if (skip) {
          const r = { id: step.id, ok: false, error: { code: 'ESKIPPED', message: `Omitido: el paso "${skip}" del que depende falló.` } };
          results.push(r); byId.set(step.id, r);
          continue;
        }

        const ev = this.policy.evaluate(step);
        this.audit.record('policy', { step: step.id, tool: step.tool, server: step.server, decision: ev.decision, reason: ev.reason, paths: ev.paths });

        if (ev.decision === Decision.DENY) {
          log.error(`${step.id} ${step.server}.${step.tool} — DENEGADO: ${ev.reason}`);
          const r = { id: step.id, ok: false, error: { code: 'EPOLICY', message: ev.reason, hint: 'Usa solo rutas dentro del sandbox y herramientas permitidas.' } };
          results.push(r); byId.set(step.id, r);
          continue;
        }
        if (ev.decision === Decision.ASK) {
          const yes = await this.approver(step, ev);
          this.audit.record('approval', { step: step.id, tool: step.tool, approved: yes });
          if (!yes) {
            log.warn(`${step.id} ${step.tool} — rechazado por el usuario`);
            const r = { id: step.id, ok: false, error: { code: 'EDENIED_BY_USER', message: 'El usuario rechazó esta operación.', hint: 'Propón una alternativa o finaliza con mcp-done.' } };
            results.push(r); byId.set(step.id, r);
            continue;
          }
        }
        if (this.policy.classify(step.tool) === 'write') this.policy.noteWrite();
        approved.push(step);
      }

      const settled = await Promise.all(
        approved.map(async (step) => {
          const t0 = Date.now();
          const res = await this.host.callTool(step.server, step.tool, step.args, 60000);
          const ms = Date.now() - t0;
          this.audit.record('tool_call', { step: step.id, server: step.server, tool: step.tool, args: step.args, ok: res.ok, ms });

          if (!res.ok) {
            log.error(`${step.id} ${step.server}.${step.tool} — ${res.error.code}: ${res.error.message} ${color.gray(ms + 'ms')}`);
            // Un hint específico del servidor (p. ej. "la línea 3 dice X") siempre
            // gana al genérico: es el que rompe los bucles de reintento.
            return { id: step.id, ok: false, error: { ...res.error, hint: res.error.hint ?? hintFor(res.error.code) } };
          }
          if (this.policy.classify(step.tool) === 'write') wrote = true;

          const { text, truncated } = truncateHeadTail(String(res.data ?? ''), this.budget.cfg.maxBytesPerResult);
          this.budget.addResultBytes(byteLength(text));
          log.ok(`${step.id} ${step.server}.${step.tool} ${color.gray(`${ms}ms · ${byteLength(text)}b${truncated ? ' (truncado)' : ''}`)}`);
          return { id: step.id, ok: true, data: text, truncated: truncated || undefined };
        })
      );

      for (const r of settled) {
        results.push(r);
        byId.set(r.id, r);
      }
    }

    // Devolver en el orden original del plan
    results.sort((a, b) => steps.findIndex((s) => s.id === a.id) - steps.findIndex((s) => s.id === b.id));
    return { results, wrote };
  }
}

function hintFor(code) {
  switch (code) {
    case 'EBADEDIT':
      return 'Usa exactamente los campos {"oldText": "...", "newText": "..."} en cada elemento de "edits".';
    case 'ENOMATCH':
      return 'Vuelve a leer el archivo con read_text_file y copia el ancla EXACTA, con su indentación.';
    case 'EAMBIGUOUS':
      return 'Amplía el fragmento oldText con más líneas de contexto para que sea único.';
    case 'EACCES':
      return 'Usa rutas relativas al root del sandbox; no salgas con "..".';
    case 'E2BIG':
      return 'Usa offset y limit para leer el archivo por partes, o usa grep.';
    case 'ENOENT':
      return 'Verifica la ruta con list_directory o directory_tree antes de leer.';
    case 'ENOTOOL':
      return 'Revisa el catálogo de herramientas del prompt inicial y usa un nombre exacto.';
    default:
      return 'Replantea el paso con argumentos distintos.';
  }
}
