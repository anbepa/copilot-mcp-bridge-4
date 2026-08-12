/**
 * Gestión del perfil del navegador.
 *
 * Historia: el perfil vivía en `<proyecto>/.browser-profile`. Cuando el usuario
 * descomprimía una versión nueva del proyecto ("copilot-mcp-bridge 2"), la carpeta
 * nueva no tenía perfil y había que iniciar sesión otra vez. La sesión no se perdía:
 * simplemente se buscaba en el sitio equivocado.
 *
 * Ahora el perfil vive en el home del usuario. Este módulo migra el antiguo si existe,
 * para que quien venga de una versión previa no tenga que volver a autenticarse.
 */
import fs from 'node:fs';
import path from 'node:path';

export const LEGACY_DIR_NAME = '.browser-profile';

/** ¿Parece un perfil de Chromium con datos reales, y no una carpeta vacía? */
export function looksLikeProfile(dir) {
  try {
    if (!fs.statSync(dir).isDirectory()) return false;
  } catch {
    return false;
  }
  // "Default" (subperfil) o "Local State" son los marcadores que Chromium siempre crea.
  return ['Default', 'Local State'].some((m) => fs.existsSync(path.join(dir, m)));
}

/**
 * Copia el perfil antiguo al nuevo si procede.
 * Es deliberadamente conservador: nunca sobrescribe un perfil existente y nunca borra
 * el antiguo. Ante la duda, no hace nada — perder una sesión es más caro que duplicarla.
 *
 * @returns {{migrated: boolean, from?: string, to?: string, reason?: string}}
 */
export function migrateLegacyProfile({ projectDir, userDataDir }) {
  const from = path.join(projectDir, LEGACY_DIR_NAME);
  const to = userDataDir;

  if (path.resolve(from) === path.resolve(to)) return { migrated: false, reason: 'same' };
  if (!looksLikeProfile(from)) return { migrated: false, reason: 'no-legacy' };
  if (fs.existsSync(to)) return { migrated: false, reason: 'target-exists' };

  try {
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.cpSync(from, to, { recursive: true, errorOnExist: false, force: true });
    return { migrated: true, from, to };
  } catch (e) {
    // Que falle la migración nunca debe impedir iniciar sesión de nuevo.
    return { migrated: false, reason: `error: ${e.message}` };
  }
}
