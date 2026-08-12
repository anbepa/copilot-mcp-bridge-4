/** Logger mínimo con colores ANSI y niveles. Cero dependencias. */

const useColor = process.stdout.isTTY && !process.env.NO_COLOR;
const c = (code) => (s) => (useColor ? `\x1b[${code}m${s}\x1b[0m` : s);

export const color = {
  dim: c('2'),
  bold: c('1'),
  red: c('31'),
  green: c('32'),
  yellow: c('33'),
  blue: c('34'),
  magenta: c('35'),
  cyan: c('36'),
  gray: c('90')
};

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40, silent: 99 };
let current = LEVELS[process.env.CMB_LOG_LEVEL] ?? LEVELS.info;

export function setLevel(name) {
  if (LEVELS[name] != null) current = LEVELS[name];
}

function emit(level, tag, args) {
  if (LEVELS[level] < current) return;
  const stream = level === 'error' || level === 'warn' ? process.stderr : process.stdout;
  stream.write(tag + ' ' + args.map(fmt).join(' ') + '\n');
}

function fmt(a) {
  if (typeof a === 'string') return a;
  try {
    return JSON.stringify(a);
  } catch {
    return String(a);
  }
}

export const log = {
  debug: (...a) => emit('debug', color.gray('  ·'), a),
  info: (...a) => emit('info', color.blue('  ›'), a),
  step: (...a) => emit('info', color.cyan('▸'), a),
  ok: (...a) => emit('info', color.green('  ✓'), a),
  warn: (...a) => emit('warn', color.yellow('  ⚠'), a),
  error: (...a) => emit('error', color.red('  ✗'), a),
  raw: (s) => process.stdout.write(s),
  banner: (title) => {
    if (LEVELS.info < current) return;
    process.stdout.write('\n' + color.bold(color.magenta('━━ ' + title + ' ')) + color.gray('─'.repeat(Math.max(0, 56 - title.length))) + '\n');
  }
};
