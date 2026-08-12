/**
 * DRIVER MOCK — permite ejecutar y probar TODO el pipeline sin navegador
 * ni sesión de Copilot. Es la pieza que hace el proyecto testeable y la que
 * debes usar para desarrollar: el 90 % de los bugs no están en el navegador.
 *
 * Dos modos:
 *  · scripted: respuestas fijas desde un JSON de escenario.
 *  · smart   : simula un planificador razonable (explora, luego edita, luego cierra).
 */
import fs from 'node:fs';
import { log, color } from '../log.mjs';

export class MockDriver {
  constructor({ scenario = null, mode = 'smart', latencyMs = 50 } = {}) {
    this.turn = 0;
    this.latencyMs = latencyMs;
    this.mode = scenario ? 'scripted' : mode;
    this.replies = scenario ? JSON.parse(fs.readFileSync(scenario, 'utf8')).replies : null;
    this.lastPrompt = '';
  }

  async init() {
    log.ok(`MockDriver activo (modo: ${this.mode}) — sin navegador`);
    return this;
  }

  async send(text) {
    this.turn++;
    this.lastPrompt = text;
    await new Promise((r) => setTimeout(r, this.latencyMs));
    const reply = this.mode === 'scripted' ? this.#scripted() : this.#smart(text);
    log.debug(color.gray(`[mock turno ${this.turn}] ${reply.slice(0, 120).replace(/\n/g, ' ')}…`));
    return reply;
  }

  #scripted() {
    const r = this.replies?.[this.turn - 1];
    if (r == null) return '```mcp-done\n{ "summary": "Escenario agotado." }\n```';
    return typeof r === 'string' ? r : JSON.stringify(r);
  }

  /** Heurística sencilla que imita a un planificador competente. */
  #smart(prompt) {
    if (this.turn === 1) {
      return [
        '```mcp-plan',
        JSON.stringify(
          {
            steps: [
              { id: 's1', server: 'unified', tool: 'search_nodes', args: { path: '.', query: 'TODO', search_content: true } },
              { id: 's2', server: 'unified', tool: 'list_directory', args: { path: '.' } },
              { id: 's3', server: 'unified', tool: 'read_file', args: { path: 'src/users.js' } }
            ],
            then: 'localizar los marcadores y leer los archivos implicados'
          },
          null,
          2
        ),
        '```'
      ].join('\n');
    }
    if (this.turn === 2) {
      return [
        '```mcp-plan',
        JSON.stringify(
          {
            steps: [
              {
                id: 'w1',
                server: 'unified',
                tool: 'edit_file',
                args: {
                  path: 'src/users.js',
                  edits: [
                    {
                      oldText: '// TODO: validar entrada del id',
                      newText:
                        '// Valida que `id` sea un entero positivo antes de construir la consulta.\n// Nota: la interpolación directa en SQL es vulnerable a inyección.'
                    }
                  ]
                }
              },
              {
                id: 'w2',
                server: 'unified',
                tool: 'edit_file',
                args: {
                  path: 'src/index.js',
                  edits: [
                    {
                      oldText: '// TODO: manejar errores de conexion',
                      newText: '// Envolver connect() en try/catch y propagar un error tipado al llamador.'
                    }
                  ]
                }
              },
              // Este paso debe ser BLOQUEADO por el Policy Engine (fuera del sandbox).
              { id: 'x1', server: 'unified', tool: 'read_file', args: { path: '../../../etc/passwd' } }
            ],
            then: 'documentar los TODO encontrados'
          },
          null,
          2
        ),
        '```'
      ].join('\n');
    }
    return [
      '```mcp-done',
      JSON.stringify(
        {
          summary: 'Se documentaron 2 marcadores TODO en src/users.js y src/index.js. Un acceso fuera del sandbox fue bloqueado por política.',
          files_changed: ['src/users.js', 'src/index.js']
        },
        null,
        2
      ),
      '```'
    ].join('\n');
  }

  async newThread() {
    return true;
  }
  async close() {}
}
