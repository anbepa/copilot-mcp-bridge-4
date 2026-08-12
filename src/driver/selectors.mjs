/**
 * Localizadores de M365 Copilot Chat — CENTRALIZADOS A PROPÓSITO.
 *
 * Esta es la única parte del sistema que se rompe cuando Microsoft cambia la UI.
 * Al estar aislada aquí, arreglar el proyecto ante un cambio de DOM es editar
 * este archivo y nada más.
 *
 * Base: localizadores validados por prueba del usuario.
 * Estrategia: aria-label + data-testid + roles ARIA. NUNCA clases CSS ofuscadas.
 */

export const SEL = {
  // ── Selección de modelo ──
  modelButton: 'xpath=//button[@aria-label="Selector de modelos"]',
  modelSubmenu: (group) =>
    `xpath=//div[@role="menuitem" and @aria-haspopup="menu"][contains(normalize-space(.),"${group}")]`,
  modelOption: (modelo) =>
    `xpath=//div[@role="menuitemradio"][contains(normalize-space(.),"${modelo}")]`,

  // ── Composer ──
  editor: '#m365-chat-editor-target-element',
  sendButton: 'button[aria-label="Enviar"]',
  // El botón sólo existe cuando hay texto; su desaparición indica "generando".

  // ── Respuesta ──
  reply: '[data-testid="copilot-message-reply-div"]',
  busyText: 'text=Generando una respuesta',

  // ── Adjuntos (canal de alto ancho de banda) ──
  fileInput: 'input[type="file"]',

  // ── Copiar respuesta en markdown crudo (más fiable que leer el DOM renderizado) ──
  copyButton:
    'xpath=(//button[contains(@aria-label,"Copiar") or contains(@aria-label,"Copy")])[last()]',

  // ── Hilo nuevo (compactación de contexto) ──
  newChatButton:
    'xpath=//button[contains(@aria-label,"Nuevo chat") or contains(@aria-label,"New chat") or contains(@title,"Nuevo chat")]'
};

/** Variantes alternativas por si cambia el idioma del tenant. */
export const SEL_FALLBACK = {
  modelButton: [
    'xpath=//button[@aria-label="Model picker"]',
    'xpath=//button[contains(@aria-label,"odel")]',
    'xpath=//button[contains(@aria-label,"odelo")]'
  ],
  sendButton: ['button[aria-label="Send"]', 'button[data-testid="send-button"]', 'button[type="submit"]'],
  busyText: ['text=Generating a response', 'text=Generando', '[data-testid="typing-indicator"]'],
  reply: ['[data-testid="copilot-message-reply"]', '[data-testid="chat-message-bot"]']
};

export const MODEL_GROUP = 'GPT';
