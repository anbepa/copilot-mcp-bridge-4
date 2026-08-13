/**
 * DRIVER PLAYWRIGHT — automatiza M365 Copilot Chat en un Chromium con perfil persistente.
 *
 * Basado en el flujo validado por el usuario:
 *   seleccionar modelo → escribir → enviar → esperar fin de streaming → extraer.
 *
 * Añadidos sobre el original:
 *   · Perfil persistente: inicias sesión UNA vez (npm run login) y se recuerda.
 *   · Canal de adjuntos: sube el Context Pack como archivo, esquivando el límite
 *     de caracteres del composer. Es la optimización de mayor impacto.
 *   · Extracción vía botón "Copiar" → markdown CRUDO, no DOM renderizado.
 *     Evita perder indentación, comillas y saltos dentro de los bloques de código.
 *   · Fallbacks de localizadores y reintentos.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { SEL, SEL_FALLBACK, MODEL_GROUP } from './selectors.mjs';
import { log, color } from '../log.mjs';

export class PlaywrightDriver {
  constructor(cfg) {
    this.cfg = cfg;
    this.browser = null;
    this.context = null;
    this.page = null;
    this.modelSelected = false;
  }

  // Instala Playwright (paquete npm + navegador Chromium) en caliente y devuelve { chromium }.
  async #autoInstallPlaywright() {
    const run = (cmd, args) => {
      log.info(color.bold(`  ↻ ${cmd} ${args.join(' ')}`));
      const r = spawnSync(cmd, args, {
        stdio: 'inherit',
        cwd: process.cwd(),
        shell: process.platform === 'win32'
      });
      if (r.status !== 0) {
        throw new Error(`Falló "${cmd} ${args.join(' ')}" (código ${r.status ?? 'desconocido'}).`);
      }
    };

    log.warn('Playwright no está instalado → intentando instalarlo automáticamente…');
    try {
      // 1) Paquete npm (idempotente si ya estuviera a medias).
      run('npm', ['install', 'playwright']);
      // 2) Binario del navegador Chromium.
      run('npx', ['--yes', 'playwright', 'install', 'chromium']);
    } catch (e) {
      throw new Error(
        'No se pudo instalar Playwright automáticamente: ' + e.message + '\n' +
          'Instálalo a mano:\n' +
          '   npm install playwright\n' +
          '   npx playwright install chromium\n' +
          '(o desactiva la autoinstalación con PLAYWRIGHT_NO_AUTOINSTALL=1)'
      );
    }

    // Reintentar el import ya con el paquete disponible.
    try {
      const mod = await import('playwright');
      log.info(color.bold('  ✓ Playwright instalado correctamente.'));
      return mod.chromium;
    } catch (e) {
      throw new Error(
        'Playwright se instaló pero no se pudo importar: ' + e.message
      );
    }
  }

  async init({ loginOnly = false } = {}) {
    let chromium;
    try {
      ({ chromium } = await import('playwright'));
    } catch {
      // Autoinstalación: si Playwright falta, lo instalamos y reintentamos.
      const autoOff =
        this.cfg?.autoInstall === false ||
        process.env.PLAYWRIGHT_NO_AUTOINSTALL === '1';
      if (autoOff) {
        throw new Error(
          'Playwright no está instalado. Ejecuta:\n' +
            '   npm install playwright\n' +
            '   npx playwright install chromium'
        );
      }
      chromium = await this.#autoInstallPlaywright();
    }

    if (this.cfg.cdpEndpoint) {
      // Modo "adjuntarse a tu Chrome ya abierto":
      //   chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-cmb
      log.info(`Conectando por CDP a ${this.cfg.cdpEndpoint}`);
      this.browser = await chromium.connectOverCDP(this.cfg.cdpEndpoint);
      this.context = this.browser.contexts()[0] ?? (await this.browser.newContext());
    } else {
      const userDataDir = path.resolve(this.cfg.userDataDir);
      fs.mkdirSync(userDataDir, { recursive: true });
      this.context = await chromium.launchPersistentContext(userDataDir, {
        headless: this.cfg.headless && !loginOnly,
        viewport: { width: 1440, height: 900 },
        args: ['--disable-blink-features=AutomationControlled']
      });
    }

    this.context.setDefaultTimeout(30000);
    this.page = this.context.pages()[0] ?? (await this.context.newPage());

    // El portapapeles necesita permiso explícito para la extracción en crudo.
    try {
      await this.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(this.cfg.url).origin });
    } catch {
      log.debug('No se pudieron conceder permisos de portapapeles; se usará extracción por DOM.');
    }

    log.info(`Navegando a ${this.cfg.url}`);
    await this.page.goto(this.cfg.url, { waitUntil: 'domcontentloaded', timeout: 60000 });

    if (loginOnly) {
      log.banner('INICIA SESIÓN EN LA VENTANA DEL NAVEGADOR');
      process.stdout.write(
        'Autentícate con tu cuenta corporativa y espera a que cargue el chat.\n' +
          'Cuando esté listo, pulsa ENTER aquí para guardar la sesión.\n'
      );
      await new Promise((r) => process.stdin.once('data', r));

      // No basta con el ENTER: hay que COMPROBAR que la sesión es real. Si el
      // composer no está, el perfil guardado no sirve y el próximo `run` fallará.
      const ok = await this.page
        .waitForSelector(SEL.editor, { state: 'visible', timeout: 20000 })
        .then(() => true)
        .catch(() => false);
      if (ok) {
        await this.page.waitForTimeout(2000); // deja que se asienten cookies/tokens
        // Ojo: aquí solo sabemos que el chat CARGÓ, no que la sesión vaya a
        // sobrevivir al reinicio. Eso lo comprueba `login` después de cerrar.
        log.ok('Chat detectado. Comprobando ahora que la sesión persista…');
      } else {
        log.warn('No se detectó el chat cargado. La sesión puede no haberse guardado.');
        log.warn('Vuelve a ejecutar `npm run login` y espera a ver la caja de texto ANTES de pulsar ENTER.');
      }
      return this;
    }

    await this.#waitForEditor();
    if (this.cfg.model) await this.selectModel(this.cfg.model).catch((e) => log.warn(`No se pudo fijar el modelo: ${e.message}`));
    return this;
  }

  async #waitForEditor() {
    log.info('Esperando el composer del chat…');
    const firstWait = this.cfg.editorTimeoutMs ?? 60000;
    if (await this.page.waitForSelector(SEL.editor, { state: 'visible', timeout: firstWait }).then(() => true).catch(() => false)) {
      log.ok('Chat listo');
      return;
    }

    // No está el composer. ¿Es una pantalla de login o es que cambió el DOM?
    // Distinguirlo importa: fallar en seco obliga a repetir todo el arranque.
    const onLogin = await this.#looksLikeLogin();
    if (onLogin && !this.cfg.headless && process.stdin.isTTY) {
      log.banner('SE REQUIERE INICIAR SESIÓN');
      process.stdout.write(
        'La sesión caducó o aún no estaba guardada.\n' +
          'Autentícate en la ventana del navegador; el puente continuará solo al detectar el chat.\n'
      );
      // Espera larga: MFA corporativo puede tardar minutos.
      if (await this.page.waitForSelector(SEL.editor, { state: 'visible', timeout: 300000 }).then(() => true).catch(() => false)) {
        await this.page.waitForTimeout(1500);
        log.ok('Sesión iniciada. Continuando.');
        return;
      }
    }

    if (onLogin && this.cfg.headless) {
      throw new Error(
        'Se requiere iniciar sesión, pero estás en modo headless y no hay ventana donde autenticarte.\n' +
          '   Ejecuta primero: npm run login   (y comprueba con: node src/cli.mjs session)\n' +
          '   Después ya podrás usar --headless con la sesión guardada.'
      );
    }
    throw new Error(
      (onLogin
        ? `Sigues en la pantalla de inicio de sesión (${this.page.url()}). Ejecuta \`npm run login\` y espera a VER la caja de texto antes de pulsar ENTER.`
        : `No apareció la caja de texto del chat en ${this.page.url()}.`) +
        '\n   Otras causas: la URL o el localizador cambiaron → revisa driver.url en config y SEL.editor en src/driver/selectors.mjs.' +
        (this.cfg.headless ? '\n   Sugerencia: repite sin --headless para ver qué muestra la página.' : '')
    );
  }

  /** Heurística: ¿estamos en una pantalla de autenticación de Microsoft? */
  async #looksLikeLogin() {
    try {
      const url = this.page.url();
      if (/login\.microsoftonline\.com|login\.live\.com|\/oauth2\/|signin/i.test(url)) return true;
      return await this.page.locator('input[type="password"], input[name="loginfmt"], #i0116, #idSIButton9').first().isVisible({ timeout: 2000 }).catch(() => false);
    } catch {
      return false;
    }
  }

  /**
   * Devuelve el primer localizador visible de una lista de candidatos.
   * Es lo que da tolerancia a cambios de idioma del tenant y a rotación de DOM.
   */
  async #firstVisible(selectors, timeoutMs = 8000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      for (const s of selectors) {
        try {
          const loc = this.page.locator(s).first();
          if ((await loc.count()) > 0 && (await loc.isVisible())) return loc;
        } catch {
          /* localizador inválido: probar el siguiente */
        }
      }
      await this.page.waitForTimeout(200);
    }
    return null;
  }

  /** A) SELECCIONAR MODELO */
  async selectModel(modelo) {
    const btn = await this.#firstVisible([SEL.modelButton, ...SEL_FALLBACK.modelButton], 8000);
    if (!btn) {
      log.warn('Selector de modelos no encontrado; se usa el modelo por defecto del tenant.');
      return false;
    }

    // Si ya está seleccionado el modelo que queremos, no toques nada.
    const actual = await this.#currentModelLabel(btn);
    if (actual && this.#sameModel(actual, modelo)) {
      this.modelSelected = true;
      log.ok(`Modelo ya activo: ${color.bold(actual)}`);
      return true;
    }

    await btn.click();
    await this.page.waitForTimeout(800); // Espera a que el menú se despliegue

    // El grupo "GPT" suele ser un submenú flyout que se abre por HOVER, no por
    // clic (al hacer clic a veces se cierra). Probamos hover y, si hace falta, clic.
    const submenu = this.page.locator(SEL.modelSubmenu(MODEL_GROUP)).first();
    try {
      await submenu.waitFor({ state: 'visible', timeout: 5000 });
      await submenu.hover();
      await this.page.waitForTimeout(400);
      // Si tras el hover la opción aún no aparece, intentamos clic para expandir.
      const optProbe = this.page.locator(SEL.modelOption(modelo)).first();
      if (!(await optProbe.isVisible().catch(() => false))) {
        await submenu.click().catch(() => {});
        await this.page.waitForTimeout(500);
      }
    } catch {
      log.debug('Sin submenú GPT; puede que las opciones sean planas.');
    }

    const opcion = this.page.locator(SEL.modelOption(modelo)).first();
    try {
      await opcion.waitFor({ state: 'visible', timeout: 5000 });
      await opcion.scrollIntoViewIfNeeded().catch(() => {});
      await opcion.click();
      await this.page.waitForTimeout(800); // Estabilización tras selección
      if (await this.#verifyModel(btn, modelo)) {
        this.modelSelected = true;
        log.ok(`Modelo: ${color.bold(modelo)}`);
        return true;
      }
      log.debug('Clic hecho pero la verificación no confirmó el cambio; reintentando por coincidencia.');
    } catch {
      // Antes de rendirnos: puede ser que el nombre difiera en mayúsculas o
      // espacios ("GPT 5.6 Think" vs "GPT-5.6 Think"). Comparamos normalizado.
      const opciones = await this.#listModelOptions();
      const norm = (s) => s.toLowerCase().replace(/[\s\-_.]+/g, '');
      const parecido = opciones.find((o) => norm(o) === norm(modelo)) ??
        opciones.find((o) => norm(o).includes(norm(modelo)) || norm(modelo).includes(norm(o)));

      if (parecido) {
        try {
          await this.page.locator(SEL.modelOption(parecido)).first().click({ timeout: 4000 });
          await this.page.waitForTimeout(800);
          this.modelSelected = true;
          log.ok(`Modelo: ${color.bold(parecido)}`);
          if (parecido !== modelo) log.debug(`(pediste "${modelo}"; el menú lo llama "${parecido}")`);
          return true;
        } catch {}
      }

      log.warn(`Modelo "${modelo}" no disponible. Continuando con el modelo por defecto del tenant.`);
      if (opciones.length) {
        log.info(`Modelos de tu menú: ${opciones.join(' · ')}`);
        log.info(`Fija el que quieras con:  --model "${opciones[0]}"   (o driver.model en config/local.json)`);
      } else {
        log.debug('No se pudo leer la lista de modelos: puede que el menú no llegara a abrirse.');
      }
      await this.page.keyboard.press('Escape').catch(() => {});
      return false;
    }
  }

  /** Lee los nombres de modelo visibles en el menú abierto (para diagnóstico). */
  async #listModelOptions() {
    try {
      const items = await this.page.locator('[role="menuitemradio"], [role="menuitem"]').allInnerTexts();
      return [...new Set(items.map((t) => t.split('\n')[0].trim()).filter((t) => t && t.length < 60))];
    } catch {
      return [];
    }
  }

  /** Compara dos nombres de modelo ignorando espacios, guiones y mayúsculas. */
  #sameModel(a, b) {
    const norm = (s) => (s || '').toLowerCase().replace(/[\s\-_.]+/g, '');
    return norm(a) === norm(b) || norm(a).includes(norm(b)) || norm(b).includes(norm(a));
  }

  /** Lee la etiqueta del modelo actualmente activo desde el botón selector. */
  async #currentModelLabel(btn) {
    try {
      const aria = (await btn.getAttribute('aria-label')) || '';
      const txt = (await btn.innerText().catch(() => '')) || '';
      // El aria-label suele ser "Selector de modelos"; el texto visible lleva el modelo.
      const raw = `${txt} ${aria}`.trim();
      return raw.split('\n')[0].trim();
    } catch {
      return '';
    }
  }

  /**
   * Verifica que el modelo realmente quedó seleccionado. Reabre el menú y
   * comprueba qué opción tiene aria-checked="true"; si no, cae al texto del botón.
   */
  async #verifyModel(btn, modelo) {
    // 1) Vía fiable: el radio marcado en el menú (si sigue abierto o al reabrir).
    try {
      const checked = await this.page
        .locator('[role="menuitemradio"][aria-checked="true"]')
        .first()
        .innerText({ timeout: 1500 });
      if (checked) return this.#sameModel(checked.split('\n')[0], modelo);
    } catch {}
    // 2) Respaldo: el texto del propio botón selector.
    const label = await this.#currentModelLabel(btn);
    if (label && this.#sameModel(label, modelo)) return true;
    // 3) Sin evidencia clara: no afirmamos éxito.
    return false;
  }

  /**
   * B+C) ENVIAR y EXTRAER.
   * @param {string} text
   * @param {{attachment?:string|null}} opts
   * @returns {Promise<string>} respuesta en markdown
   */
  async send(text, { attachment = null } = {}) {
    if (this.cfg.selectModelEachTurn && this.cfg.model) {
      await this.selectModel(this.cfg.model).catch(() => {});
    }

    const before = await this.page.locator(SEL.reply).count();

    if (attachment && this.cfg.attachments?.enabled) {
      await this.#attach(attachment);
    }

    // B) Escribir en el composer
    const caja = this.page.locator(SEL.editor);
    await caja.click();
    await this.#fillEditor(caja, text);

    // El botón "Enviar" solo existe cuando hay texto.
    const send = await this.#firstVisible([SEL.sendButton, ...SEL_FALLBACK.sendButton], 10000);
    if (!send) throw new Error('El botón Enviar no apareció. ¿Se escribió el texto en el composer?');
    await send.click();

    // C) Esperar fin de streaming y extraer
    return await this.#waitAndExtract(before);
  }

  /**
   * Escribir en un editor rich-text (contenteditable) es el punto frágil clásico:
   * asignar .value o .innerText NO actualiza el estado interno de React.
   * Hay que ir por eventos de entrada reales.
   */
  async #fillEditor(caja, text) {
    try {
      await caja.fill(text);
      const got = (await caja.innerText().catch(() => '')) ?? '';
      if (got.trim().length >= Math.min(20, text.length / 2)) return;
    } catch {
      log.debug('fill() falló; usando inserción por eventos.');
    }

    // Plan B: insertar vía portapapeles (rápido y respeta saltos de línea)
    try {
      await this.page.evaluate((t) => navigator.clipboard.writeText(t), text);
      await caja.click();
      await this.page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V');
      await this.page.waitForTimeout(300);
      const got = (await caja.innerText().catch(() => '')) ?? '';
      if (got.trim().length > 0) return;
    } catch {
      log.debug('Pegado por portapapeles falló; usando execCommand.');
    }

    // Plan C: insertText nativo, que sí dispara beforeinput/input
    await caja.click();
    await this.page.evaluate(
      ({ sel, t }) => {
        const el = document.querySelector(sel);
        if (!el) return;
        el.focus();
        document.execCommand('insertText', false, t);
      },
      { sel: SEL.editor, t: text }
    );
  }

  /** Sube el Context Pack como archivo: esquiva el límite de caracteres del composer. */
  async #attach(filePath) {
    const abs = path.resolve(filePath);
    if (!fs.existsSync(abs)) return;
    try {
      const input = this.page.locator(SEL.fileInput).first();
      await input.setInputFiles(abs, { timeout: 8000 });
      log.ok(`Adjunto: ${path.basename(abs)} (${fs.statSync(abs).size} bytes)`);
      // Dar tiempo a que la UI procese la subida antes de enviar.
      await this.page.waitForTimeout(2500);
      return true;
    } catch (e) {
      log.warn(`No se pudo adjuntar (${e.message}). El contexto irá en el cuerpo del prompt.`);
      return false;
    }
  }

  /**
   * Espera de fin de streaming — lógica del usuario, endurecida.
   * El único indicador fiable de "ocupado" es el texto "Generando una respuesta";
   * lo combinamos con estabilidad del texto durante `quietMs`.
   */
  async #waitAndExtract(prevReplyCount) {
    const { timeoutMs, quietMs, pollMs } = this.cfg;
    const loc = this.page.locator(SEL.reply).last();
    const start = Date.now();
    let ultimo = '';
    let estableDesde = Date.now();
    let sawNew = false;

    while (Date.now() - start < timeoutMs) {
      let ocupado = false;
      for (const s of [SEL.busyText, ...SEL_FALLBACK.busyText]) {
        if ((await this.page.locator(s).count().catch(() => 0)) > 0) {
          ocupado = true;
          break;
        }
      }

      const count = await this.page.locator(SEL.reply).count().catch(() => 0);
      if (count > prevReplyCount) sawNew = true;

      let actual = '';
      try {
        actual = (await loc.innerText()).replace(/^Copilot said:\s*/i, '').trim();
      } catch {}
      const vacio = actual.length === 0 || /Generando una respuesta|Generating a response/i.test(actual);

      if (!ocupado && !vacio && sawNew) {
        if (actual === ultimo) {
          if (Date.now() - estableDesde >= quietMs) break; // streaming terminado
        } else {
          ultimo = actual;
          estableDesde = Date.now();
        }
      } else {
        if (actual && !vacio) ultimo = actual;
        estableDesde = Date.now();
      }
      await this.page.waitForTimeout(pollMs);
    }

    // Extracción en CRUDO por portapapeles: preserva las vallas ``` y su contenido.
    if (this.cfg.preferClipboard) {
      const raw = await this.#extractViaCopy();
      if (raw && raw.length >= ultimo.length * 0.6) return raw;
    }
    return ultimo;
  }

  async #extractViaCopy() {
    try {
      const btn = this.page.locator(SEL.copyButton).first();
      if ((await btn.count()) === 0) return null;
      await btn.click({ timeout: 4000 });
      await this.page.waitForTimeout(400);
      const text = await this.page.evaluate(() => navigator.clipboard.readText());
      return typeof text === 'string' && text.trim() ? text.trim() : null;
    } catch {
      return null;
    }
  }

  /** Abre un hilo nuevo: compactación de contexto entre tareas largas. */
  async newThread() {
    try {
      const btn = this.page.locator(SEL.newChatButton).first();
      if ((await btn.count()) > 0) {
        await btn.click();
        await this.page.waitForTimeout(1500);
        return true;
      }
    } catch {}
    await this.page.goto(this.cfg.url, { waitUntil: 'domcontentloaded' });
    await this.#waitForEditor();
    return true;
  }

  /**
   * Cierre ORDENADO. Importa más de lo que parece: Chromium escribe cookies,
   * localStorage e IndexedDB (donde MSAL guarda los tokens de Microsoft) al
   * apagarse. Si el proceso muere antes de ese volcado, el perfil queda sin
   * sesión y el siguiente arranque pide login de nuevo.
   */
  async close({ flushMs = 1200 } = {}) {
    try {
      // Deja que se completen las escrituras pendientes del perfil.
      if (this.page && !this.page.isClosed()) await this.page.waitForTimeout(flushMs).catch(() => {});
      if (this.browser) await this.browser.close();
      else if (this.context) await this.context.close();
      // Chromium desacopla el apagado: un margen extra evita perfiles truncados.
      await new Promise((r) => setTimeout(r, 400));
    } catch {}
  }

  /**
   * ¿Sobrevive la sesión a un reinicio del navegador? Es la única comprobación que
   * vale: reabre el perfil y replica exactamente lo que hará `run`.
   *
   * `headless` importa: algunos tenants con Conditional Access rechazan navegadores
   * sin interfaz. Por eso el llamador puede reintentar con ventana para distinguir
   * "la sesión murió" de "headless está bloqueado" — son problemas muy distintos.
   *
   * Devuelve un ESTADO, no un booleano. La diferencia importa: "vi la pantalla de
   * login" (la sesión murió) y "se me acabó el tiempo" (no lo sé) son cosas
   * distintas, y tratarlas igual hace que le digas a alguien que repita un MFA
   * corporativo sin necesidad. Solo afirmamos que la sesión murió cuando lo vemos.
   *
   * @returns {Promise<{status:'alive'|'expired'|'unknown', detail?:string}>}
   */
  async verifySessionDetailed({ timeoutMs, headless = true } = {}) {
    // El mismo margen que usa `run`: si el composer tarda 60 s en aparecer al
    // ejecutar, verificar con 25 s producía falsos negativos en tenants lentos.
    const editorTimeout = timeoutMs ?? this.cfg.editorTimeoutMs ?? 60000;

    let chromium;
    try {
      ({ chromium } = await import('playwright'));
    } catch {
      return { status: 'unknown', detail: 'Playwright no está instalado' };
    }

    const userDataDir = path.resolve(this.cfg.userDataDir);
    let ctx = null;
    try {
      ctx = await this.#launchWithRetry(chromium, userDataDir, headless);
    } catch (e) {
      // No hemos podido ni abrir el navegador: no sabemos nada de la sesión.
      return { status: 'unknown', detail: `no se pudo abrir el perfil: ${e.message.split('\n')[0]}` };
    }

    try {
      const page = ctx.pages()[0] ?? (await ctx.newPage());
      try {
        await page.goto(this.cfg.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      } catch (e) {
        return { status: 'unknown', detail: `no se pudo cargar ${this.cfg.url}: ${e.message.split('\n')[0]}` };
      }

      // Carrera: gana lo que aparezca antes, el chat o la pantalla de login.
      const editor = page
        .waitForSelector(SEL.editor, { state: 'visible', timeout: editorTimeout })
        .then(() => 'alive')
        .catch(() => null);
      const login = page
        .waitForSelector('input[type="password"], input[name="loginfmt"], #i0116', {
          state: 'visible',
          timeout: editorTimeout
        })
        .then(() => 'expired')
        .catch(() => null);

      const winner = await Promise.race([
        editor,
        login,
        new Promise((r) => setTimeout(() => r(null), editorTimeout + 2000))
      ]);
      if (winner === 'alive') return { status: 'alive' };
      if (winner === 'expired') return { status: 'expired', detail: 'apareció la pantalla de login' };

      // Nadie ganó: segunda opinión por la URL antes de rendirnos.
      const url = page.url();
      if (/login\.microsoftonline\.com|login\.live\.com|\/oauth2\//i.test(url)) {
        return { status: 'expired', detail: `redirigido a ${url}` };
      }
      return { status: 'unknown', detail: `no apareció el chat en ${editorTimeout / 1000}s (url: ${url})` };
    } finally {
      if (ctx) await ctx.close().catch(() => {});
      await new Promise((r) => setTimeout(r, 300));
    }
  }

  /**
   * Chromium deja un cerrojo (SingletonLock) en el perfil y tarda un instante en
   * soltarlo tras cerrarse. Relanzar de inmediato falla; reintentar, no.
   */
  async #launchWithRetry(chromium, userDataDir, headless, intentos = 3) {
    let last;
    for (let i = 0; i < intentos; i++) {
      try {
        return await chromium.launchPersistentContext(userDataDir, {
          headless,
          viewport: { width: 1440, height: 900 },
          args: ['--disable-blink-features=AutomationControlled']
        });
      } catch (e) {
        last = e;
        if (!/singleton|profile|lock|in use/i.test(e.message)) throw e;
        await new Promise((r) => setTimeout(r, 1500 * (i + 1)));
      }
    }
    throw last;
  }

  /** Compatibilidad: booleano simple. `alive` es lo único que cuenta como sí. */
  async verifySession(opts = {}) {
    return (await this.verifySessionDetailed(opts)).status === 'alive';
  }
}
