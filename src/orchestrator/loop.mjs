/**
 * BUCLE PRINCIPAL — patrón "oráculo por lotes", no agente iterativo.
 *
 *   Turno 1 : Context Pack + tarea  →  Copilot devuelve un PLAN completo
 *   Puente  : ejecuta el plan EN PARALELO contra MCP (con política y aprobación)
 *   Turno 2 : resultados agregados  →  Copilot devuelve más plan o DONE
 *
 * Objetivo de diseño: 2-4 turnos por tarea, no 20-30.
 */
import { parseReply, renderBlock } from '../protocol/blocks.mjs';
import { validatePlan, validateAction } from '../protocol/validate.mjs';
import { buildBootstrapPrompt, buildResultsPrompt, buildRepairPrompt } from '../protocol/prompt.mjs';
import { Budget } from './budget.mjs';
import { PlanExecutor } from './executor.mjs';
import { log, color } from '../log.mjs';

export class Orchestrator {
  #failedSignatures;

  constructor({ driver, host, policy, config, approver, audit, contextPack, roots }) {
    this.driver = driver;
    this.host = host;
    this.policy = policy;
    this.config = config;
    this.approver = approver;
    this.audit = audit;
    this.contextPack = contextPack;
    this.roots = roots;
    /** Firmas de pasos fallidos → nº de intentos. Detecta bucles de reintento. */
    this.#failedSignatures = new Map();
    this.budget = new Budget(config.budget);
    this.executor = new PlanExecutor({
      host, policy, budget: this.budget, approver, audit,
      defaultServer: Object.keys(host.clients)[0] ?? [...host.clients.keys()][0]
    });
    this.transcript = [];
  }

  async run(task) {
    const defaultServer = [...this.host.clients.keys()][0];
    this.executor.defaultServer = defaultServer;

    let message = buildBootstrapPrompt({
      task,
      toolCatalog: this.host.catalogForPrompt(),
      contextPack: this.contextPack.pack,
      roots: this.roots,
      attachmentName: this.contextPack.attachmentName
    });
    let attachment = this.contextPack.attachmentPath ?? null;
    let repairs = 0;
    const filesChanged = new Set();

    while (true) {
      const stop = this.budget.exhausted();
      if (stop) {
        log.warn(`Presupuesto agotado: ${stop}`);
        return this.#finish('budget', stop, filesChanged);
      }

      const turn = this.budget.nextTurn();
      log.banner(`TURNO ${turn}/${this.config.budget.maxTurns} → Copilot`);
      log.debug('prompt bytes:', Buffer.byteLength(message));

      const t0 = Date.now();
      const reply = await this.driver.send(message, { attachment });
      const ms = Date.now() - t0;
      attachment = null; // el adjunto solo viaja en el primer turno
      log.info(`respuesta en ${color.bold(ms + 'ms')} · ${Buffer.byteLength(reply ?? '')} bytes`);
      this.audit.record('copilot_turn', { turn, promptBytes: Buffer.byteLength(message), replyBytes: Buffer.byteLength(reply ?? ''), ms });
      this.transcript.push({ turn, prompt: message, reply });

      const parsed = parseReply(reply);
      // La UI de Copilot suele borrar la etiqueta del bloque al renderizarlo
      // ("mcp-plan no es totalmente compatible… Plain Text"), así que inferir por
      // la forma es el caso NORMAL, no una anomalía. A debug para no ensuciar.
      if (parsed.inferred) log.debug('Bloque sin etiqueta mcp-*; se infirió por su forma.');

      // ── DONE ──
      if (parsed.kind === 'done') {
        const summary = parsed.value?.summary ?? '(sin resumen)';
        // NO confiamos en `files_changed` del modelo: la fuente de verdad es
        // nuestro registro de escrituras ejecutadas con éxito. Si el modelo
        // afirma cambios que no ocurrieron, lo señalamos.
        const claimed = parsed.value?.files_changed ?? [];
        const phantom = claimed.filter((f) => !filesChanged.has(f));
        if (phantom.length) {
          log.warn(`Copilot afirma haber cambiado archivos que NO se escribieron: ${phantom.join(', ')}`);
          this.audit.record('phantom_claim', { claimed, actual: [...filesChanged] });
        }
        log.banner('TAREA COMPLETADA');
        process.stdout.write(color.green(summary) + '\n');
        return this.#finish('done', summary, filesChanged);
      }

      // ── ASK ──
      if (parsed.kind === 'ask') {
        log.banner('COPILOT NECESITA UNA DECISIÓN');
        process.stdout.write(color.yellow(parsed.value?.question ?? '(sin pregunta)') + '\n');
        return this.#finish('ask', parsed.value?.question ?? '', filesChanged);
      }

      // ── PLAN / ACTION ──
      let steps = null;
      let vErrors = null;
      if (parsed.kind === 'plan' && parsed.value) {
        const v = validatePlan(parsed.value, { maxSteps: this.config.budget.maxStepsPerPlan });
        if (v.ok) steps = v.value.steps;
        else vErrors = v.errors;
        if (v.ok && parsed.value.then) log.info(color.gray('intención: ' + parsed.value.then));
      } else if (parsed.kind === 'action' && parsed.value) {
        const v = validateAction(parsed.value);
        if (v.ok) steps = [v.value];
        else vErrors = v.errors;
      }

      if (!steps) {
        repairs++;
        const reason = parsed.error ?? (vErrors ? vErrors.join('; ') : 'formato no reconocido');
        this.audit.record('parse_failure', { turn, reason, replyPreview: (reply ?? '').slice(0, 500) });
        if (repairs > this.config.budget.maxRepairAttempts) {
          log.error('Se agotaron los intentos de reparación de formato.');
          return this.#finish('parse_error', reason, filesChanged);
        }
        log.warn(`Formato inválido (${reason}). Reparando… intento ${repairs}/${this.config.budget.maxRepairAttempts}`);
        message = buildRepairPrompt(reason, repairs);
        continue;
      }
      repairs = 0;

      const { results } = await this.executor.execute(steps);

      // Solo cuentan como modificados los pasos de escritura que REALMENTE
      // se ejecutaron con éxito: los denegados por política o rechazados por
      // el usuario no deben aparecer en el informe final.
      for (const r of results) {
        if (!r.ok) continue;
        const step = steps.find((s) => s.id === r.id);
        if (step && this.policy.classify(step.tool) === 'write') {
          // dryRun previsualiza: NO toca el disco y no debe reportarse como cambio.
          if (step.args?.dryRun === true) continue;
          const p = step.args?.path ?? step.args?.destination;
          if (p) filesChanged.add(p);
        }
      }

      // Cortacircuitos: si el modelo repite un paso que ya falló idéntico, seguirá
      // fallando. Detectarlo y decírselo evita quemar todo el presupuesto de turnos.
      const repeated = [];
      for (const r of results) {
        if (r.ok) continue;
        const step = steps.find((s) => s.id === r.id);
        if (!step) continue;
        const sig = `${step.server}.${step.tool}:${JSON.stringify(step.args)}`;
        const prev = this.#failedSignatures.get(sig) ?? 0;
        this.#failedSignatures.set(sig, prev + 1);
        if (prev > 0) repeated.push(`${step.tool} con los mismos argumentos (${prev + 1} intentos)`);
      }
      if (repeated.length) {
        log.warn(`Repitiendo pasos ya fallidos: ${repeated.join('; ')}`);
      }

      message = buildResultsPrompt({
        results,
        turn,
        maxTurns: this.config.budget.maxTurns,
        budgetNote: this.budget.note(),
        repeated
      });
    }
  }

  #finish(status, detail, filesChanged) {
    const out = {
      status,
      detail,
      filesChanged: [...filesChanged],
      budget: this.budget.summary(),
      transcript: this.transcript
    };
    this.audit.record('task_end', { status, detail, filesChanged: out.filesChanged, budget: out.budget });
    return out;
  }
}

export { renderBlock };
