/**
 * Cómo interpretar dos intentos de verificación de sesión (headless y con ventana).
 *
 * Esto existe por un fallo real: `login` dijo "la sesión NO persistió" y acto seguido
 * `run` entró sin pedir credenciales. La verificación tenía un timeout de 25 s mientras
 * que `run` usaba 60 s, así que un M365 lento producía un falso negativo.
 *
 * La lección: "no lo he podido confirmar" NO es lo mismo que "ha fallado". Confundirlos
 * hace que alguien repita un MFA corporativo sin ninguna necesidad. Solo afirmamos que
 * la sesión murió cuando hemos VISTO la pantalla de login.
 */

/** @typedef {{status:'alive'|'expired'|'unknown', detail?:string}} Check */

/**
 * @param {Check} headless  resultado del intento sin ventana
 * @param {Check} [headed]  resultado del reintento con ventana (si lo hubo)
 * @returns {{outcome:'alive'|'expired'|'unknown', exitCode:number, headlessBlocked:boolean, detail?:string}}
 */
export function decideSessionOutcome(headless, headed = null) {
  // 1. Si algún intento vio el chat, la sesión está viva. Fin.
  if (headless?.status === 'alive') {
    return { outcome: 'alive', exitCode: 0, headlessBlocked: false };
  }
  if (headed?.status === 'alive') {
    // Funcionó con ventana pero no sin ella: puede ser lentitud o un tenant que
    // bloquea headless. En ambos casos el consejo es el mismo: usa --headed.
    return { outcome: 'alive', exitCode: 0, headlessBlocked: true };
  }

  // 2. Ver la pantalla de login es la ÚNICA prueba de que la sesión murió.
  const expired = [headless, headed].find((r) => r?.status === 'expired');
  if (expired) {
    return { outcome: 'expired', exitCode: 1, headlessBlocked: false, detail: expired.detail };
  }

  // 3. Ni chat ni login: no lo sabemos. Salimos con 0 para no romper scripts ni
  //    alarmar: lo más probable es que solo fuera lentitud.
  return {
    outcome: 'unknown',
    exitCode: 0,
    headlessBlocked: false,
    detail: (headed ?? headless)?.detail ?? 'sin detalle'
  };
}
