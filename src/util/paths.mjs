/**
 * Utilidades de rutas y contención de sandbox.
 * Toda validación de seguridad de rutas pasa por aquí.
 */
import path from 'node:path';
import fs from 'node:fs';

/** Normaliza a ruta absoluta resolviendo symlinks cuando el destino existe. */
export function realish(p) {
  const abs = path.resolve(p);
  try {
    return fs.realpathSync(abs);
  } catch {
    // El destino puede no existir todavía (p.ej. write_file). Resolvemos el padre.
    const parent = path.dirname(abs);
    try {
      return path.join(fs.realpathSync(parent), path.basename(abs));
    } catch {
      return abs;
    }
  }
}

/** ¿`child` está contenido en `parent`? Robusto frente a `..` y symlinks. */
export function isInside(parent, child) {
  const p = realish(parent);
  const c = realish(child);
  if (p === c) return true;
  const rel = path.relative(p, c);
  return rel.length > 0 && !rel.startsWith('..') && !path.isAbsolute(rel);
}

/** Primer root que contiene la ruta, o null. */
export function containingRoot(roots, target) {
  for (const r of roots) if (isInside(r, target)) return r;
  return null;
}

/** Convierte un glob sencillo (*, **, ?) a RegExp. */
export function globToRegExp(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') {
        // ** => cualquier cosa, incluidas barras
        re += '.*';
        i++;
        if (glob[i + 1] === '/') i++; // **/ absorbe la barra
      } else {
        re += '[^/]*';
      }
    } else if (c === '?') re += '[^/]';
    else if ('\\^$+.()|{}[]'.includes(c)) re += '\\' + c;
    else re += c;
  }
  return new RegExp('^' + re + '$');
}

export function matchesAnyGlob(relPath, globs) {
  const norm = relPath.split(path.sep).join('/');
  return globs.some((g) => globToRegExp(g).test(norm) || globToRegExp(g).test('/' + norm));
}

/** Ruta relativa POSIX respecto a un root (para mostrar al modelo). */
export function toDisplay(root, abs) {
  return path.relative(realish(root), realish(abs)).split(path.sep).join('/') || '.';
}
