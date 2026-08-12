/** Presupuestos duros. Sin esto, un bucle mal planteado consume tu cuota del tenant. */
export class Budget {
  constructor(cfg) {
    this.cfg = cfg;
    this.turn = 0;
    this.totalResultBytes = 0;
    this.startedAt = Date.now();
  }

  nextTurn() {
    this.turn++;
    return this.turn;
  }

  get elapsedSeconds() {
    return Math.round((Date.now() - this.startedAt) / 1000);
  }

  addResultBytes(n) {
    this.totalResultBytes += n;
  }

  /** @returns {null|string} motivo de parada */
  exhausted() {
    if (this.turn >= this.cfg.maxTurns) return `Límite de ${this.cfg.maxTurns} turnos alcanzado`;
    if (this.totalResultBytes >= this.cfg.maxTotalResultBytes)
      return `Límite de contexto alcanzado (${this.totalResultBytes} bytes de resultados)`;
    if (this.elapsedSeconds >= this.cfg.maxTaskSeconds)
      return `Límite de tiempo alcanzado (${this.elapsedSeconds}s)`;
    return null;
  }

  /** Aviso que se inyecta al modelo cuando quedan pocos turnos. */
  note() {
    const left = this.cfg.maxTurns - this.turn;
    if (left <= 2) return `⚠️ Quedan ${left} turnos. Prioriza cerrar la tarea y emitir \`\`\`mcp-done.`;
    return null;
  }

  summary() {
    return {
      turns: this.turn,
      seconds: this.elapsedSeconds,
      resultBytes: this.totalResultBytes
    };
  }
}
