#!/usr/bin/env node
/**
 * CLI del puente.
 *
 *   node src/cli.mjs doctor
 *   node src/cli.mjs login
 *   node src/cli.mjs run --task "…"                    (Copilot real)
 *   node src/cli.mjs run --driver mock --task "…"      (sin navegador)
 *   node src/cli.mjs pack --task "…"                   (solo el Context Pack)
 */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { loadConfig } from './config.mjs';
import { log, color, setLevel } from './log.mjs';
import { McpHost } from './mcp/host.mjs';
import { ContextCompiler } from './context/compiler.mjs';
import { ManifestCache } from './context/cache.mjs';
import { PolicyEngine } from './policy/engine.mjs';
import { Orchestrator } from './orchestrator/loop.mjs';
import { migrateLegacyProfile } from './util/profile.mjs';
import { decideSessionOutcome } from './util/session.mjs';
import { createApprover } from './util/approve.mjs';
import { Audit } from './audit.mjs';
import { createDriver } from './driver/index.mjs';

const argv = process.argv.slice(2);
const cmd = argv[0] ?? 'help';

function flag(name, def = undefined) {
  const i = argv.indexOf('--' + name);
  if (i === -1) return def;
  const next = argv[i + 1];
  if (next === undefined || next.startsWith('--')) return true;
  return next;
}

if (flag('verbose', false) || flag('debug', false)) setLevel('debug');
if (flag('quiet', false)) setLevel('warn');

const commands = { doctor, login, session, run, pack, help };
(commands[cmd] ?? help)().catch((e) => {
  log.error(e.message);
  if (process.env.CMB_LOG_LEVEL === 'debug') console.error(e);
  process.exit(1);
});

// ───────────────────────────────────────────────────────────────

async function help() {
  process.stdout.write(`
${color.bold('copilot-mcp-bridge')} — puente M365 Copilot Chat ⇄ MCP local

${color.bold('COMANDOS')}
  doctor                    Verifica entorno, MCP y sandbox
  login                     Inicia sesión y VERIFICA que la sesión persiste
  session                   Comprueba si la sesión guardada sigue siendo válida
  pack   --task "..."       Compila y muestra el Context Pack (sin llamar a Copilot)
  run    --task "..."       Ejecuta una tarea completa

${color.bold('OPCIONES DE run')}
  --task <texto>            Tarea a resolver (obligatorio)
  --driver <playwright|mock>  Motor. 'mock' no necesita navegador     [playwright]
  --scenario <archivo>      Escenario JSON de respuestas para el mock
  --model <nombre>          Modelo a seleccionar           ["GPT 5.6 Think"]
  --root <ruta>             Root del sandbox                    [./workspace]
  --max-turns <n>           Presupuesto de turnos                        [8]
  --max-seconds <n>         Límite de tiempo total en segundos         [900]
  --yes                     Auto-aprueba escrituras ${color.yellow('(solo demos)')}
  --headless                Sin ventana ${color.gray('(requiere sesión ya guardada)')}
  --headed                  Fuerza mostrar la ventana
  --profile <ruta>          Perfil del navegador   [~/.copilot-mcp-bridge/browser-profile]
  --no-attach               Envía el contexto en el prompt, sin adjunto
  --verbose                 Log detallado

${color.bold('EJEMPLOS')}
  npm run demo
  node src/cli.mjs run --driver mock --task "Documenta los TODO de src/"
  node src/cli.mjs login                    ${color.gray('# una sola vez')}
  node src/cli.mjs session                  ${color.gray('# ¿sigue viva la sesión?')}
  node src/cli.mjs run --task "Documenta los TODO de src/"
  node src/cli.mjs run --headless --task "Revisa src/db.js"   ${color.gray('# sin ventana')}
`);
}

async function doctor() {
  log.banner('DIAGNÓSTICO');
  const cfg = loadConfig();

  const major = Number(process.versions.node.split('.')[0]);
  major >= 20 ? log.ok(`Node ${process.version}`) : log.error(`Node ${process.version} — se requiere >= 20`);

  for (const r of cfg.sandbox.roots) {
    fs.existsSync(r) ? log.ok(`Sandbox root: ${r}`) : log.error(`Sandbox root NO existe: ${r}`);
  }

  try {
    await import('playwright');
    log.ok('playwright instalado');
  } catch {
    log.warn('playwright NO instalado → el driver "mock" sí funciona. Para el real: npm install playwright && npx playwright install chromium');
  }

  maybeMigrateProfile(cfg);
  if (fs.existsSync(cfg.driver.userDataDir)) {
    log.ok(`Perfil de navegador: ${cfg.driver.userDataDir}`);
    log.info('Para comprobar que la sesión sigue viva: node src/cli.mjs session');
  } else {
    log.warn(`Sin perfil de navegador (${cfg.driver.userDataDir}). Ejecuta: npm run login`);
  }

  const host = new McpHost({ servers: cfg.mcp.servers, roots: cfg.sandbox.roots, cwd: cfg.projectDir });
  await host.start();
  const cat = host.catalog();
  log.ok(`Catálogo MCP: ${cat.length} herramientas de ${host.clients.size} servidor(es)`);

  const policy = new PolicyEngine({ policy: cfg.policy, sandbox: cfg.sandbox, roots: cfg.sandbox.roots });
  const escape = policy.evaluate({ server: 'fs', tool: 'read_text_file', args: { path: '../../etc/passwd' } });
  escape.decision === 'deny' ? log.ok('Policy Engine bloquea escapes del sandbox') : log.error('¡FALLO DE SEGURIDAD! El escape del sandbox no fue bloqueado');
  const secret = policy.evaluate({ server: 'fs', tool: 'read_text_file', args: { path: '.env' } });
  secret.decision === 'deny' ? log.ok('Policy Engine bloquea archivos sensibles (.env)') : log.error('¡FALLO! .env no está bloqueado');
  const write = policy.evaluate({ server: 'fs', tool: 'write_file', args: { path: 'a.txt', content: 'x' } });
  write.decision === 'ask' ? log.ok('Las escrituras requieren aprobación humana') : log.warn(`Escrituras: ${write.decision}`);

  await host.stop();
  log.banner('OK');
}

async function login() {
  const over = { driver: { headless: false } };
  if (flag('profile')) over.driver.userDataDir = String(flag('profile'));
  const cfg = loadConfig(over);
  maybeMigrateProfile(cfg);
  const driver = createDriver('playwright', cfg.driver);
  await driver.init({ loginOnly: true });

  // Cierre ordenado: Chromium vuelca los tokens al perfil AL APAGARSE.
  // Antes hacíamos process.exit(0) aquí y eso truncaba la sesión.
  log.info('Cerrando el navegador para volcar la sesión al perfil…');
  await driver.close({ flushMs: 2500 });

  // Verificación honesta: reabrimos el perfil y comprobamos que la sesión
  // sobrevive. Decir "guardada" sin comprobarlo fue el bug de la versión anterior.
  log.info('Verificando que la sesión persiste tras reiniciar el navegador…');
  const c1 = await driver.verifySessionDetailed({ headless: true });
  let c2 = null;

  // Un fallo en headless no prueba nada: puede ser lentitud o un tenant que
  // rechaza navegadores sin interfaz. Segunda opinión con ventana.
  if (c1.status !== 'alive') {
    log.info(`Sin confirmar en headless (${c1.detail}). Reintentando con ventana…`);
    c2 = await driver.verifySessionDetailed({ headless: false });
  }

  const d = decideSessionOutcome(c1, c2);
  const r = { status: d.outcome, detail: d.detail };
  const headlessBlocked = d.headlessBlocked;

  if (r.status === 'alive') {
    log.ok('Sesión PERSISTENTE. Ya puedes ejecutar `run` sin volver a autenticarte.');
    log.info(`Perfil: ${cfg.driver.userDataDir}`);
    if (headlessBlocked) {
      log.warn('No se confirmó en headless: si `--headless` te falla, usa `--headed`.');
    } else {
      log.info('También puedes usar --headless para ejecutar sin ver el navegador.');
    }
    process.exit(0);
  }

  if (r.status === 'expired') {
    log.error('La sesión NO persistió: el próximo `run` volverá a pedir login.');
    process.stdout.write(
      '\nCausas habituales:\n' +
        '  1. Pulsaste ENTER antes de que el chat cargara del todo → repite y espera a VER la caja de texto.\n' +
        '  2. Tu tenant exige reautenticación por directiva (Conditional Access): no es evitable desde aquí.\n' +
        '  3. El perfil está en una carpeta sincronizada (OneDrive/iCloud) que interfiere con Chromium.\n' +
        `     Perfil actual: ${cfg.driver.userDataDir}\n` +
        '     Prueba otra ubicación:  node src/cli.mjs login --profile ~/.cmb-profile\n' +
        '\nAunque no persista, `run` detecta la pantalla de login y te deja autenticarte sin abortar.\n'
    );
    process.exit(1);
  }

  // status === 'unknown': no lo sabemos. Decir "no persistió" aquí sería mentir
  // y te haría repetir un MFA que probablemente no hace falta.
  log.warn(`No se pudo CONFIRMAR la sesión: ${r.detail}`);
  process.stdout.write(
    '\nEsto NO significa que haya fallado. Lo más habitual es que M365 tardara más de lo\n' +
      'esperado en cargar. La sesión probablemente esté guardada.\n' +
      `  Perfil: ${cfg.driver.userDataDir}\n` +
      '\nSiguiente paso: ejecuta tu tarea con normalidad.\n' +
      '  node src/cli.mjs run --task "..."\n' +
      'Si el chat abre sin pedirte credenciales, todo está correcto.\n' +
      'Si tu M365 es lento, sube driver.editorTimeoutMs en config/local.json.\n'
  );
  process.exit(0);
}

/** Migra el perfil antiguo (dentro del proyecto) al nuevo (en el home). */
function maybeMigrateProfile(cfg) {
  const r = migrateLegacyProfile({
    projectDir: cfg.projectDir,
    userDataDir: cfg.driver.userDataDir
  });
  if (r.migrated) {
    log.ok(`Perfil migrado a ${r.to}`);
    log.info('Ya no se perderá al actualizar el proyecto o descomprimir una versión nueva.');
  } else if (r.reason?.startsWith('error:')) {
    log.debug(`No se pudo migrar el perfil antiguo: ${r.reason}`);
  }
  return r;
}

/** Comprueba el estado de la sesión sin tocar nada. */
async function session() {
  const cfg = loadConfig(flag('profile') ? { driver: { userDataDir: String(flag('profile')) } } : {});
  log.banner('ESTADO DE LA SESIÓN');
  maybeMigrateProfile(cfg);
  log.info(`Perfil: ${cfg.driver.userDataDir}`);
  if (!fs.existsSync(cfg.driver.userDataDir)) {
    log.warn('No existe el perfil. Ejecuta: npm run login');
    process.exit(1);
  }
  const driver = createDriver('playwright', { ...cfg.driver, headless: true });
  log.info('Abriendo el chat con el perfil guardado…');
  const c1 = await driver.verifySessionDetailed({ headless: true });
  let c2 = null;
  if (c1.status !== 'alive') {
    log.info(`Sin confirmar en headless (${c1.detail}). Reintentando con ventana…`);
    c2 = await driver.verifySessionDetailed({ headless: false });
  }

  const d = decideSessionOutcome(c1, c2);
  if (d.outcome === 'alive') {
    if (d.headlessBlocked) {
      log.ok('Sesión válida (con ventana).');
      log.warn('No se confirmó en headless: si `--headless` te falla, usa `--headed`.');
    } else {
      log.ok('Sesión válida. `run` funcionará sin pedir login, incluso con --headless.');
    }
  } else if (d.outcome === 'expired') {
    log.error('Sesión caducada. Ejecuta: npm run login');
  } else {
    log.warn(`No se pudo confirmar el estado: ${d.detail}`);
    log.info('Puede ser simple lentitud de M365. Prueba a ejecutar tu tarea directamente.');
  }
  process.exit(d.exitCode);
}

async function pack() {
  const cfg = loadConfig();
  const task = String(flag('task', ''));
  const { compiler, cache } = await buildCompiler(cfg);
  const prev = await cache.load();
  const res = await compiler.compile({ task, previousManifest: prev });
  log.banner('CONTEXT PACK');
  process.stdout.write(res.pack + '\n');
  log.banner('ESTADÍSTICAS');
  process.stdout.write(JSON.stringify(res.stats, null, 2) + '\n');
}

async function run() {
  let task = flag('task');
  if (!task || task === true) throw new Error('Falta --task "descripción de la tarea"');

  // Si task es una ruta a un archivo .md, leer su contenido.
  if (typeof task === 'string' && task.endsWith('.md')) {
    const taskPath = path.resolve(task);
    if (fs.existsSync(taskPath)) {
      log.info(`Leyendo tarea desde archivo: ${taskPath}`);
      task = fs.readFileSync(taskPath, 'utf8').trim();
    } else {
      log.warn(`Se indicó un archivo .md pero no existe: ${taskPath}. Usando el texto literal.`);
    }
  }

  const overrides = { driver: {}, budget: {}, sandbox: {} };
  if (flag('model')) overrides.driver.model = String(flag('model'));
  if (flag('headless', false)) overrides.driver.headless = true;
  if (flag('headed', false)) overrides.driver.headless = false;
  if (flag('profile')) overrides.driver.userDataDir = String(flag('profile'));
  if (flag('no-attach', false)) overrides.driver.attachments = { enabled: false };
  if (flag('max-turns')) overrides.budget.maxTurns = Number(flag('max-turns'));
  if (flag('max-seconds')) overrides.budget.maxTaskSeconds = Number(flag('max-seconds'));
  if (flag('root')) overrides.sandbox.roots = [String(flag('root'))];

  const cfg = loadConfig(overrides);
  const driverKind = String(flag('driver', cfg.driver.kind));
  const autoYes = !!flag('yes', false);

  log.banner('COPILOT ⇄ MCP BRIDGE');
  log.info(`tarea    : ${task}`);
  log.info(`driver   : ${driverKind}`);
  log.info(`sandbox  : ${cfg.sandbox.roots.join(', ')}`);
  if (driverKind === 'playwright') {
    maybeMigrateProfile(cfg);
    log.info(`modo     : ${cfg.driver.headless ? 'headless (sin ventana)' : 'con ventana'}`);
    if (cfg.driver.headless) {
      // En headless no hay forma de resolver un MFA interactivo.
      log.debug('En headless no podrás autenticarte: la sesión guardada debe ser válida.');
    }
  }
  if (autoYes) log.warn('--yes activo: las escrituras NO pedirán confirmación');

  const audit = new Audit(cfg.audit);
  audit.record('task_start', { task, driver: driverKind, roots: cfg.sandbox.roots });

  // 1) Context Pack (la optimización clave: descubrimiento en 0 turnos)
  log.banner('1 · COMPILANDO CONTEXTO');
  const { compiler, cache } = await buildCompiler(cfg);
  const prev = await cache.load();
  const compiled = await compiler.compile({ task: String(task), previousManifest: prev });
  await cache.save(compiled.manifest);
  log.ok(
    `Pack: ${compiled.stats.files} archivos · ${compiled.stats.symbols} símbolos · ` +
      `${compiled.stats.todos} TODO · ${compiled.stats.bytes} bytes` +
      (compiled.stats.unchanged ? ` · ${compiled.stats.unchanged} sin cambios (caché)` : '')
  );

  const contextPack = { pack: compiled.pack, attachmentPath: null, attachmentName: null };
  const useAttachment =
    driverKind === 'playwright' &&
    cfg.driver.attachments?.enabled &&
    compiled.pack.length > (cfg.driver.attachments.thresholdChars ?? 4000);
  if (useAttachment) {
    const dir = path.join(cfg.projectDir, '.tmp');
    await fsp.mkdir(dir, { recursive: true });
    const file = path.join(dir, 'context-pack.md');
    await fsp.writeFile(file, compiled.pack, 'utf8');
    contextPack.attachmentPath = file;
    contextPack.attachmentName = 'context-pack.md';
    contextPack.pack = null;
    log.ok(`Contexto irá como adjunto (${compiled.stats.bytes} bytes) — esquiva el límite del composer`);
  }

  // 2) Servidores MCP
  log.banner('2 · ARRANCANDO MCP');
  const host = new McpHost({ servers: cfg.mcp.servers, roots: cfg.sandbox.roots, cwd: cfg.projectDir });
  await host.start();

  // 3) Driver
  log.banner('3 · CONECTANDO CON COPILOT');
  const driver = createDriver(driverKind, cfg.driver, {
    scenario: flag('scenario') ? String(flag('scenario')) : null,
    mockMode: String(flag('mock-mode', 'smart'))
  });
  await driver.init();

  // 4) Bucle
  const policy = new PolicyEngine({ policy: cfg.policy, sandbox: cfg.sandbox, roots: cfg.sandbox.roots });
  const approver = createApprover({ autoYes, roots: cfg.sandbox.roots });
  const orch = new Orchestrator({ driver, host, policy, config: cfg, approver, audit, contextPack, roots: cfg.sandbox.roots });

  let result;
  try {
    result = await orch.run(String(task));
  } finally {
    await driver.close();
    await host.stop();
  }

  log.banner('RESUMEN');
  process.stdout.write(
    `estado        : ${statusColor(result.status)}\n` +
      `turnos        : ${result.budget.turns}\n` +
      `duración      : ${result.budget.seconds}s\n` +
      `bytes result. : ${result.budget.resultBytes}\n` +
      `archivos      : ${result.filesChanged.length ? result.filesChanged.join(', ') : '(ninguno)'}\n` +
      `auditoría     : ${audit.file ?? '(desactivada)'}\n`
  );
  process.exit(result.status === 'done' ? 0 : 1);
}

function statusColor(s) {
  if (s === 'done') return color.green(s);
  if (s === 'ask') return color.yellow(s);
  return color.red(s);
}

async function buildCompiler(cfg) {
  const root = cfg.sandbox.roots[0];
  if (!fs.existsSync(root)) throw new Error(`El root del sandbox no existe: ${root}`);
  const compiler = new ContextCompiler({ root, config: cfg.context, denyGlobs: cfg.sandbox.denyGlobs });
  const cache = new ManifestCache(path.join(cfg.projectDir, '.tmp'));
  return { compiler, cache };
}
