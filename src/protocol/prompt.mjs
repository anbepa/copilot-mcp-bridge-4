/**
 * Construcción de prompts.
 *
 * Principios (ver README §Optimización):
 *  - Contrato corto, imperativo y con ejemplo. Los LLM copian ejemplos mejor que leen reglas.
 *  - Prohibir prosa: cada palabra extra es tiempo de streaming.
 *  - Un solo plan por turno; ejecución en paralelo la hace el puente.
 *  - Errores con `hint` para que el modelo se autocorrija sin gastar turnos extra.
 */

export const CONTRACT = `Eres el planificador de un puente local que ejecuta herramientas MCP en la máquina del usuario.
TÚ NO EJECUTAS NADA: describes qué ejecutar y el puente lo hace y te devuelve los resultados.

REGLAS DE FORMATO (obligatorias):
1. Responde SIEMPRE con exactamente UN bloque de código con una de estas etiquetas:
   \`\`\`mcp-plan   → lista de herramientas a ejecutar (preferido)
   \`\`\`mcp-done   → tarea terminada, con resumen
   \`\`\`mcp-ask    → necesitas una decisión del usuario para continuar
2. NO escribas texto fuera del bloque. Nada de introducciones ni despedidas.
3. Pide en UN SOLO plan todo lo que puedas: los pasos sin "depends_on" se ejecutan EN PARALELO.
4. Usa rutas relativas al root del sandbox. Nunca rutas absolutas ni "..".
5. Para leer archivos grandes usa offset/limit. Para buscar usa grep, no leas todo.
6. Para modificar archivos usa edit_file con anclas de texto EXACTAS y ÚNICAS (incluye la indentación).
   Nunca uses write_file para cambios pequeños.
   Los campos se llaman EXACTAMENTE "oldText" y "newText". NO uses "search"/"replace".
7. Si un resultado llega con "ok": false, lee el "hint" y corrige en el siguiente plan.
   Si el hint dice que el problema son los NOMBRES DE CAMPO, no vuelvas a leer el archivo: corrige los nombres.
8. No repitas un paso que ya falló sin cambiar algo concreto.

FORMATO DE EDICIÓN (el error más común: memorízalo):
\`\`\`mcp-plan
{
  "steps": [
    { "id": "e1", "server": "fs", "tool": "edit_file",
      "args": { "path": "src/db.js",
                "edits": [ { "oldText": "// TODO: mover credenciales", "newText": "// Credenciales desde process.env" } ] } }
  ],
  "then": "documento el TODO"
}
\`\`\`

FORMATO DE PLAN:
\`\`\`mcp-plan
{
  "steps": [
    { "id": "s1", "server": "fs", "tool": "grep", "args": { "path": ".", "pattern": "TODO" } },
    { "id": "s2", "server": "fs", "tool": "read_text_file", "args": { "path": "src/index.js" } }
  ],
  "then": "con esto identificaré los TODO y propondré las ediciones"
}
\`\`\`

FORMATO FINAL:
\`\`\`mcp-done
{ "summary": "qué se hizo, en 1-3 frases", "files_changed": ["src/a.js"] }
\`\`\``;

export function buildBootstrapPrompt({ task, toolCatalog, contextPack, roots, attachmentName }) {
  const parts = [];
  parts.push(CONTRACT);
  parts.push('\n## HERRAMIENTAS DISPONIBLES\n' + toolCatalog.join('\n'));
  parts.push(`\n## SANDBOX\nRoots accesibles (todo lo demás está bloqueado): ${roots.join(', ')}`);

  if (attachmentName) {
    parts.push(
      `\n## CONTEXTO\nEl archivo adjunto **${attachmentName}** contiene el árbol del proyecto, el mapa de símbolos y los archivos clave ya precargados. Úsalo antes de pedir lecturas: la mayoría de la información que necesitas ya está ahí.`
    );
  } else if (contextPack) {
    parts.push('\n## CONTEXTO PRECARGADO\n' + contextPack);
  }

  parts.push(`\n## TAREA\n${task}`);
  parts.push('\nResponde ahora con un único bloque ```mcp-plan.');
  return parts.join('\n');
}

export function buildResultsPrompt({ results, turn, maxTurns, budgetNote, repeated = [] }) {
  const lines = [];
  lines.push('```mcp-results');
  lines.push(JSON.stringify({ turn, results }, null, 2));
  lines.push('```');
  lines.push('');
  if (repeated.length) {
    // Señal explícita contra el bucle: repetir lo mismo dará el mismo error.
    lines.push(
      `⚠️ Estás repitiendo pasos que ya fallaron: ${repeated.join('; ')}. ` +
        'NO los repitas otra vez: lee el "hint" del error y cambia algo concreto (los nombres de campo, el ancla o la herramienta). ' +
        'Si no puedes avanzar, responde con ```mcp-ask explicando qué necesitas.'
    );
    lines.push('');
  }
  lines.push(
    `Turno ${turn}/${maxTurns}. Continúa: emite \`\`\`mcp-plan si necesitas más herramientas, o \`\`\`mcp-done si la tarea está completa. Sin texto fuera del bloque.`
  );
  if (budgetNote) lines.push(budgetNote);
  return lines.join('\n');
}

export function buildRepairPrompt(reason, attempt) {
  return [
    `⚠️ No pude interpretar tu respuesta anterior (intento ${attempt}).`,
    `Motivo: ${reason}`,
    '',
    'Reenvía SOLO un bloque de código válido, sin ninguna palabra fuera de él:',
    '```mcp-plan',
    '{ "steps": [ { "id": "s1", "server": "fs", "tool": "grep", "args": { "path": ".", "pattern": "TODO" } } ] }',
    '```',
    'o bien:',
    '```mcp-done',
    '{ "summary": "..." }',
    '```'
  ].join('\n');
}

export function buildDeniedPrompt(denials) {
  return [
    '```mcp-results',
    JSON.stringify({ results: denials }, null, 2),
    '```',
    '',
    'Algunas acciones fueron BLOQUEADAS por la política local o rechazadas por el usuario. No insistas con la misma acción: replantea usando solo rutas dentro del sandbox y herramientas permitidas, o emite ```mcp-done explicando qué no se pudo hacer.'
  ].join('\n');
}
