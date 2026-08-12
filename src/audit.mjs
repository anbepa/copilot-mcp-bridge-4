/** Log de auditoría append-only en JSONL. Todo lo que toca disco queda registrado. */
import fs from 'node:fs';
import path from 'node:path';

export class Audit {
  constructor({ dir, enabled = true }) {
    this.enabled = enabled;
    this.dir = dir;
    this.sessionId = new Date().toISOString().replace(/[:.]/g, '-');
    if (enabled) {
      fs.mkdirSync(dir, { recursive: true });
      this.file = path.join(dir, `session-${this.sessionId}.jsonl`);
    }
  }

  record(event, data = {}) {
    if (!this.enabled) return;
    const line = JSON.stringify({ ts: new Date().toISOString(), session: this.sessionId, event, ...data });
    try {
      fs.appendFileSync(this.file, line + '\n', 'utf8');
    } catch {
      /* la auditoría nunca debe romper la ejecución */
    }
  }
}
