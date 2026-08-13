/**
 * BUCLE PRINCIPAL — patrón "oráculo por lotes", no agente iterativo.
 *
 *   Turno 1 : Context Pack + tarea  →  Copilot devuelve un PLAN completo
 *   Puente  : ejecuta el plan EN PARALELO contra MCP (con política y aprobación)
 *   Turno 2 : resultados agregados  →  Copilot devuelve más plan o DONE
 *
 * Objetivo de diseño: 2-4 turnos por tarea, no 20-30.
 *
 * MODO CHAT (runChat): el mismo bucle, pero cuando una tarea termina (done/ask/
 * budget/parse_error) NO se cierra el proceso: se pide al usuario un nuevo
 * mensaje por la terminal y se reinyecta como siguiente tarea, reutilizando el
 * mismo hilo de Copilot para mantener el contexto conversacional. El presupuesto
 * de turnos se reinicia por cada mensaje del usuario, para que el chat pueda
 * durar indefinidamente sin agotarse tras la primera tarea.
 */
import { parseReply, renderBlock } from '../protocol/blocks.mjs';
import { validatePlan, validateAction } from '../protocol/validate.mjs';
import { buildBootstrapPrompt, buildResultsPrompt, buildRepairPrompt, buildUserReplyPrompt } from '../protocol/prompt.mjs';
import { Budget } from './budget.mjs';
import { PlanExecutor } from './executor.mjs';
import { log, color } from '../log.mjs';
import { ask } from '../util/approve.mjs';

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
    this.interactive = !!config.interactive;
    /** Firmas de pasos fallidos → nº de intentos. Detecta bucles de reintento. */
    this.#failedSignatures = new Map();
    this.budget = new Budget(config.budget);
    this.executor = new PlanExecutor({
      host, policy, budget: this.budget, approver, audit,
      defaultServer: Object.keys(host.clients)[0] ?? [...host.clients.keys()][0]
    });
    this.transcript = [];
    /** Se pone a true en cuanto el primer mensaje viaja a Copilot (con su adjunto). */
    this.bootstrapped = false;
  }

  /**
   * Ejecuta UNA tarea de principio a fin (comportamiento clásico).
   * Firma pública estable: la usan el CLI (`run`) y los tests E2E.
   */
  async run(task) {
    return this.runTask(String(task));
  }

  /**
   * MODO CHAT: REPL conversacional. Ejecuta una tarea, y al terminar pide al
   * usuario el siguiente mensaje mediante `promptUser()`. Continúa hasta que el
   * usuario pida salir (o `promptUser` devuelva null/"").
   *
   * @param {object}   opts
   * @param {string}  [opts.firstTask]  Primer mensaje (si ya se conoce). Si falta, se pide.
   * @param {() => Promise<string|null>} opts.promptUser  Devuelve el próximo mensaje del usuario.
   * @returns {Promise<{status:string, tasks:number, budget:object, transcript:Array}>}
   */
  async runChat({ firstTask = null, promptUser }) {
    if (typeof promptUser !== 'function') {
      throw new Error('runChat requiere una función promptUser()');
    }

    let tasks = 0;
    let lastStatus = 'idle';
    const allFilesChanged = new Set();

    // Primer mensaje: el pasado por parámetro o, si no, pedido al usuario.
    let task = firstTask != null ? String(firstTask).trim() : await this.#nextUserMessage(promptUser);

    while (task) {
      tasks++;
      // Cada mensaje del usuario arranca con presupuesto de turnos fresco.
      // El coste real (bytes/tiempo) sí es acumulativo si el config lo define,
      // pero los turnos se renuevan para no "morir" tras la primera respuesta.
      this.budget = new Budget(this.config.budget);
      this.executor.budget = this.budget;

      const res = await this.runTask(task);
      lastStatus = res.status;
      for (const f of res.filesChanged) allFilesChanged.add(f);

      // Si el modelo pidió una decisión (ask) y estamos en interactivo, runTask
      // ya la resolvió inline. Aquí solo encadenamos el siguiente mensaje libre.
      task = await this.#nextUserMessage(promptUser);
    }

    log.info('Sesión de chat finalizada.');
    return {
      status: lastStatus,
      tasks,
      filesChanged: [...allFilesChanged],
      budget: this.budget.summary(),
      transcript: this.transcript
    };
  }

  /** Pide el siguiente mensaje; normaliza salidas y comandos de salida. */
  async #nextUserMessage(promptUser) {
    const raw = await promptUser();
    const trimmed = (raw ?? '').trim();
    if (!trimmed) return null;
    if (/^(salir|exit|quit|q|:q|stop|terminar)$/i.test(trimmed)) return null;
    return trimmed;
  }

  /**
   * Bucle de una sola tarea. Devuelve el resultado en lugar de terminar el
   * proceso, para que `runChat` pueda encadenar tareas.
   */
  async runTask(task) {
    const defaultServer = [...this.host.clients.keys()][0];
    this.executor.defaultServer = defaultServer;

    let message;
    let attachment = null;
    // El bootstrap (contrato + catálogo + context pack) solo se envía la primera
    // vez. En mensajes posteriores del chat, el hilo ya tiene el contrato: basta
    // con reinyectar la nueva instrucción del usuario para ahorrar tokens/turnos.
    if (!this.bootstrapped) {
      message = buildBootstrapPrompt({
        task,
        toolCatalog: this.host.catalogForPrompt(),
        contextPack: this.contextPack.pack,
        roots: this.roots,
        attachmentName: this.contextPack.attachmentName
      });
      attachment = this.contextPack.attachmentPath ?? null;
      this.bootstrapped = true;
    } else {
      message = buildUserReplyPrompt(task);
    }

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
        const question = parsed.value?.question ?? '(sin pregunta)';
        log.banner('COPILOT NECESITA UNA DECISIÓN');
        process.stdout.write(color.yellow(question) + '\n');

        // Modo interactivo (tipo chat): en vez de terminar, preguntamos al
        // usuario en la terminal y reinyectamos su respuesta para continuar.
        if (this.interactive) {
          const answer = await ask(
            `\n${color.bold('Tu respuesta')} ${color.gray('(escribe "salir" para terminar)')}: `,
            { raw: true }
          );
          const trimmed = (answer ?? '').trim();
          if (!trimmed || /^(salir|exit|quit|q|stop|terminar)$/i.test(trimmed)) {
            log.info('Sesión interactiva finalizada por el usuario.');
            return this.#finish('ask', question, filesChanged);
          }
          this.audit.record('user_reply', { turn, question, answer: trimmed });
          message = buildUserReplyPrompt(trimmed);
          attachment = null;
          continue;
        }

        return this.#finish('ask', question, filesChanged);
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
