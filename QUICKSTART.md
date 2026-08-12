# Arranque rápido — 5 minutos

**Requisito:** Node.js 20 o superior (`node --version`). Nada más.

## Paso 1 · Comprobar que funciona (sin navegador, sin red)

```bash
cd copilot-mcp-bridge
npm install       # no descarga nada: el núcleo tiene CERO dependencias
npm test          # 67 tests
npm run doctor    # entorno + MCP + reglas de seguridad
npm run demo      # pipeline COMPLETO con driver simulado
```

Y las dos regresiones de los fallos reales que ya están corregidos:

```bash
npm run demo:searchreplace   # Copilot usa {search,replace} → antes: bucle infinito. Ahora: OK
npm run demo:loop            # el modelo repite un paso fallido → cortacircuitos
```

Si `npm run demo` termina en `estado: done`, el núcleo funciona. Verás:
plan → ejecución paralela → un paso bloqueado por política → diffs → resumen.

Comprueba lo que ocurrió:

```bash
cat workspace/src/users.js     # archivo modificado
cat .audit/*.jsonl             # auditoría completa
```

Restaura el sandbox cuando quieras con `git checkout workspace/` (o vuelve a descomprimir el zip).

---

## Paso 2 · Ver el Context Pack

```bash
node src/cli.mjs pack --task "Documenta los TODO de src/"
```

Esto es exactamente lo que Copilot recibe en el turno 1. **Entiéndelo bien**: es la
optimización que convierte 10 turnos de exploración en 0.

---

## Paso 3 · Conectar con Copilot real

```bash
npm install playwright
npx playwright install chromium
npm run login
```

Se abre Chromium. Inicia sesión con tu cuenta corporativa.

> ⚠️ **Espera a VER la caja de texto del chat antes de pulsar ENTER.** Si pulsas antes, la
> sesión se guarda a medias.

Al pulsar ENTER el puente **no se fía de ti**: cierra el navegador ordenadamente (Chromium
vuelca los tokens al perfil solo al apagarse), lo **reabre** y comprueba que la sesión
sigue viva. Si no persistió, te lo dice y sale con error en vez de fingir que funcionó.

Compruébalo cuando quieras, sin lanzar ninguna tarea:

```bash
node src/cli.mjs session
```

### Dónde vive el perfil (importante)

El perfil se guarda en **`~/.copilot-mcp-bridge/browser-profile`**, es decir en tu carpeta
personal, **fuera del proyecto**.

> 📌 Antes vivía en `./.browser-profile`, dentro del proyecto. Por eso al descomprimir una
> versión nueva (`copilot-mcp-bridge 2`) aparecía un perfil vacío y había que iniciar sesión
> de cero. Ahora el perfil sobrevive a actualizaciones, cambios de carpeta y descargas nuevas.
> Si tenías un perfil antiguo dentro del proyecto, `login` lo migra automáticamente.

Para usar otro perfil (p. ej. dos cuentas):

```bash
node src/cli.mjs login --profile ~/.cmb-cuenta-b
node src/cli.mjs run    --profile ~/.cmb-cuenta-b --task "..."
```

Si la sesión caduca, `run` detecta la pantalla de login y te deja autenticarte sin abortar
(hasta 5 minutos para el MFA), en lugar de fallar.

---

## Paso 4 · Calibrar (no te lo saltes)

```bash
npm run calibrate
```

Mide los 4 números de los que depende todo: límite del composer, si funcionan los adjuntos,
latencia por turno y si el modelo respeta el formato. Tarda ~2 minutos y evita que
rediseñes después. Resultados en `config/calibration.json`.

---

## Paso 5 · Primera tarea real

```bash
node src/cli.mjs run --task "Lee src/db.js y explica qué problemas de seguridad tiene"
```

Empieza con una tarea de **solo lectura**. Cuando confíes en el bucle, prueba escrituras:

```bash
node src/cli.mjs run --task "Documenta con comentarios los TODO de src/"
```

Cada escritura te mostrará el diff y pedirá confirmación. **No uses `--yes` con código real.**

---

## Paso 6 · Ejecutar sin ver el navegador (headless)

Una vez la sesión está guardada, añade `--headless` y el navegador no se muestra:

```bash
node src/cli.mjs run --headless --task "Documenta los TODO de src/"
```

También de forma permanente en `config/local.json`, o por entorno con `CMB_HEADLESS=1`.

Tres cosas que conviene saber:

1. **El login debe hacerse con ventana.** En headless no hay dónde teclear la contraseña ni
   resolver el MFA. Haz `npm run login` una vez y a partir de ahí ya puedes ir sin ventana.
   Si intentas `--headless` sin sesión válida, el puente lo detecta y te lo dice claramente
   en lugar de quedarse esperando hasta agotar el tiempo.
2. **Las aprobaciones se siguen pidiendo** en la terminal. Headless oculta el navegador, no
   el control humano sobre las escrituras.
3. Algunos tenants con Conditional Access bloquean navegadores headless. Si `--headless`
   falla pero con ventana funciona, es eso: usa `--headed` (o quita el flag).

| Quiero… | Comando |
|---|---|
| Ver qué hace (primeras veces) | `node src/cli.mjs run --task "..."` |
| Que no moleste en segundo plano | `node src/cli.mjs run --headless --task "..."` |
| Forzar ventana pese a la config | `node src/cli.mjs run --headed --task "..."` |
| Saber si la sesión sigue viva | `node src/cli.mjs session` |

---

## Si algo falla

**Consulta `TROUBLESHOOTING.md`**: documenta cada fallo observado en ejecuciones reales con su
causa raíz y su arreglo. Resumen rápido:

| Síntoma | Causa y solución |
|---|---|
| `No apareció la caja de texto` | Sesión no guardada → `npm run login` y **espera a ver el chat** antes del ENTER |
| Te pide login otra vez | Descomprimiste una versión nueva del proyecto. **Ya corregido**: el perfil vive en `~/.copilot-mcp-bridge/`. Verifica con `node src/cli.mjs session` |
| `--headless` no encuentra el chat | En headless no puedes autenticarte. Haz `npm run login` con ventana una vez |
| `Playwright no está instalado` | `npm install playwright && npx playwright install chromium` |
| `ENOMATCH` al editar | Corregido en esta versión. Si persiste, el ancla no existe de verdad: es correcto que falle |
| `mcp-plan no es compatible` | No es un error: solo es el resaltado de sintaxis de la UI |
| `Formato inválido` repetido | Sube `budget.maxRepairAttempts` o cambia de modelo |
| Respuestas cortadas | Sube `driver.timeoutMs` y `driver.quietMs` |
| Modelo no seleccionable | Pon el nombre exacto de tu menú en `driver.model`, o `null` |
| Adjunto no funciona | `--no-attach` y baja `context.maxPackBytes` |
| Todo denegado por política | Revisa `sandbox.roots` en `config/default.json` |

Para diagnóstico detallado: añade `--verbose` a cualquier comando.

---

## Configuración

No edites `config/default.json`. Crea **`config/local.json`** con solo lo que cambies:

```json
{
  "sandbox": { "roots": ["/ruta/a/tu/proyecto"] },
  "driver":  { "model": "GPT-4o", "headless": false },
  "budget":  { "maxTurns": 6 }
}
```

---

## Recordatorio de seguridad

Este puente mueve contenido entre tu tenant corporativo y tu disco local.
Antes de apuntarlo a código real de tu organización, **valida con Seguridad**.
Empieza siempre con el sandbox de ejemplo.
