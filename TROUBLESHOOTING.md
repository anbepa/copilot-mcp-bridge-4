# Resolución de problemas

Casos observados en ejecuciones reales, con su causa y su arreglo.

---

## 1. Tuve que iniciar sesión otra vez después de `npm run login`

**Síntoma**

```
✓ Sesión guardada en /Users/.../.browser-profile
...
✗ No apareció la caja de texto del chat.
```

Este fallo apareció **dos veces** en ejecuciones reales, y cada vez por un motivo distinto.
La segunda vez destapó la causa de fondo. Las tres causas están corregidas.

### Causa A — el perfil vivía dentro del proyecto ⭐ la causa real

La pista estaba en el prompt del log: la carpeta era `copilot-mcp-bridge 2`. Es decir, se
había descomprimido **una versión nueva** del proyecto. Como `userDataDir` era
`./.browser-profile` — *dentro* del proyecto — cada descarga nueva empezaba con un perfil
vacío. Ningún arreglo del código de login podía evitarlo: el perfil simplemente no estaba ahí.

**Arreglado:** el perfil ahora vive en **`~/.copilot-mcp-bridge/browser-profile`**, en tu
carpeta personal. Sobrevive a actualizaciones, a descomprimir zips nuevos y a ejecutar desde
otro directorio. Si tenías un perfil antiguo dentro del proyecto, `login` lo **migra solo**.

Para usar otro perfil (dos cuentas, pruebas): `--profile ~/.cmb-otra-cuenta`.

### Causa B — el navegador moría antes de guardar los tokens

`login` hacía `process.exit(0)` inmediatamente después de cerrar el contexto. Chromium vuelca
cookies, `localStorage` e IndexedDB (donde MSAL guarda los tokens de Microsoft) **al apagarse**,
y ese apagado quedaba truncado. El mensaje "✓ Sesión guardada" era literalmente falso.

**Arreglado:** cierre ordenado con margen de volcado (`flushMs`) antes de terminar el proceso.

### Causa C — se daba por buena una sesión a medias

Si pulsabas ENTER antes de que el chat cargara, se guardaba un perfil incompleto.

**Arreglado:** `login` ya no se fía del ENTER. Cierra el navegador, lo **reabre** y comprueba
que la sesión persiste de verdad. Si no persiste, **sale con código de error** y enumera las
causas probables, en vez de decirte que todo fue bien.

### Cómo verificarlo tú

```bash
npm run login              # verifica solo; falla si la sesión no persistió
node src/cli.mjs session   # ¿sigue viva? Sin lanzar ninguna tarea
node src/cli.mjs doctor    # muestra la ruta del perfil y si existe
```

**Nota:** si tu tenant aplica Conditional Access con reautenticación forzada, ningún arreglo
puede evitar que te pida login periódicamente. Eso es política corporativa, no un bug.

Además, `run` ya **no falla en seco**: si detecta una pantalla de login te deja autenticarte en
la ventana y continúa (hasta 5 minutos, suficiente para MFA).

---

## 2. Bucle infinito de `ENOMATCH` al editar archivos ⭐ el más importante

**Síntoma** (del log real):

```
✗ s2 fs.edit_file — ENOMATCH: La edición #1 no encontró su texto ancla en src/db.js.
   Vuelve a leer el archivo y usa un fragmento exacto y único
✓ s3 fs.read_text_file        ← lo relee
✗ s4 fs.edit_file — ENOMATCH: ...  ← y vuelve a fallar igual
```

**Causa raíz.** Copilot envió los campos con otro nombre:

```json
{ "search": "// TODO: ...", "replace": "// ..." }   ← lo que envió Copilot
{ "oldText": "// TODO: ...", "newText": "// ..." }  ← lo que esperaba el servidor
```

`ed.oldText` era `undefined`, así que la búsqueda fallaba siempre. Y el mensaje de error era
**activamente engañoso**: decía "vuelve a leer el archivo" cuando el archivo no tenía nada malo.
Copilot obedecía, releía, y volvía a fallar por la misma razón — hasta agotar los 8 turnos.

**Arreglado con cuatro capas:**

1. **Alias aceptados.** `search/replace`, `old_string/new_string`, `oldStr/newStr`,
   `find/replaceWith`, `from/to`, `before/after`… todos funcionan (`src/util/edits.mjs`).
2. **Diagnóstico honesto.** Si los campos no se reconocen, el error ya no es `ENOMATCH` sino
   `EBADEDIT`, dice **qué campos llegaron** y su hint empieza por *"NO vuelvas a leer el archivo"*.
3. **Coincidencia tolerante.** Si el ancla solo difiere en espacios o indentación, se aplica igual.
4. **Cortacircuitos.** Si el modelo repite un paso idéntico que ya falló, el puente lo detecta y
   se lo advierte explícitamente en vez de dejarlo consumir el presupuesto.

Compruébalo tú mismo — este escenario reproduce tu fallo exacto:

```bash
npm run demo:searchreplace   # antes: bucle hasta agotar turnos. Ahora: resuelto en 3 turnos.
npm run demo:loop            # verifica el cortacircuitos anti-repetición
```

---

## 3. "mcp-plan no es totalmente compatible. El resaltado se basa en Plain Text"

**No es un error.** Es la UI de Copilot avisando de que no conoce ese lenguaje para colorear.
El bloque llega igual y el puente lo interpreta por su forma.

Antes esto imprimía `⚠ Bloque sin etiqueta mcp-*` en cada turno. Como es el comportamiento
**normal** de la UI, ese aviso pasó a nivel debug. Para verlo: `--verbose`.

---

## 4. `login` dice "la sesión NO persistió" pero `run` funciona igual

**Esto era un bug del verificador, no de tu sesión.** Corregido.

`run` esperaba hasta 60 s a que cargara el chat, pero la verificación de `login` solo esperaba
25 s. En un tenant lento el chat no llegaba a tiempo y se daba la sesión por muerta — aunque
estuviera perfectamente guardada, como demostraba el `run` siguiente entrando sin credenciales.

**Arreglado:** la verificación usa el mismo timeout que `run` y distingue tres estados:

| Estado | Qué significa | Qué hace |
|---|---|---|
| `alive` | Se vio el chat | Todo bien, sale con 0 |
| `expired` | **Se vio la pantalla de login** | Sesión caducada, sale con 1 |
| `unknown` | Ni una cosa ni otra (lentitud, cerrojo del perfil…) | Te avisa, **sale con 0** |

La regla: solo se afirma que la sesión murió cuando se ha **visto** la pantalla de login.
Un "no lo sé" no es un "ha fallado" — confundirlos te hacía repetir un MFA innecesario.

Si ves `No se pudo CONFIRMAR la sesión`, simplemente ejecuta tu tarea. Si el chat abre sin
pedirte credenciales, todo está bien. Si tu M365 es lento de forma habitual, súbelo en
`config/local.json`:

```json
{ "driver": { "editorTimeoutMs": 90000 } }
```

---

## 5. `Modelo "X" no disponible en el menú`

No es fatal: el puente continúa con el modelo por defecto de tu tenant. Pero conviene
arreglarlo, porque el modelo por defecto suele seguir peor el formato del protocolo y te
cuesta turnos de reparación.

Ahora el puente reintenta **normalizando** el nombre (ignora mayúsculas, espacios, guiones y
puntos), así `GPT 5.6 Think` encuentra a `GPT-5.6 Think`. Si aun así no lo halla, **te lista
los modelos que sí tiene tu menú**. Copia el nombre exacto:

```bash
node src/cli.mjs run --model "GPT-5.6 Think" --task "..."
```

O fíjalo en `config/local.json`: `{ "driver": { "model": "..." } }`.
Con `{ "driver": { "model": null } }` no toca el selector y usa el que tengas puesto.

---

## 6. `--headless` no encuentra el chat (pero con ventana sí funciona)

`--headless` oculta el navegador. Ya existía, y sigue disponible en `run`:

```bash
node src/cli.mjs run --headless --task "Documenta los TODO de src/"
```

Dos motivos por los que puede fallar:

**a) No hay sesión válida.** En headless no hay ventana donde teclear la contraseña ni
resolver el MFA. Haz `npm run login` **con ventana** una vez; después ya puedes ir headless.
El puente detecta este caso y te lo dice explícitamente en lugar de esperar hasta agotar el
tiempo con un error genérico.

**b) Conditional Access bloquea headless.** Algunos tenants rechazan navegadores sin interfaz.

Esto lo detectan solos `login` y `session`: si la verificación falla en headless, **reintentan
con ventana** antes de dar la sesión por muerta. Si funciona con ventana, te avisan de que tu
tenant bloquea headless en vez de acusar en falso a una sesión que sí es válida. Un diagnóstico
equivocado te haría repetir un MFA que no hacía ninguna falta.

Recuerda: **headless no desactiva las aprobaciones**. Los diffs se siguen pidiendo en la
terminal. Oculta el navegador, no el control humano sobre las escrituras.

---

## 7. Otros

| Síntoma | Causa y solución |
|---|---|
| `Playwright no está instalado` | `npm install playwright && npx playwright install chromium` |
| `Formato inválido` repetido | Sube `budget.maxRepairAttempts` a 3, o prueba otro modelo |
| Respuestas cortadas a medias | Sube `driver.timeoutMs` y `driver.quietMs` (modelos "Think" tardan más) |
| El modelo no se selecciona | Pon el nombre exacto de tu menú en `driver.model`, o `null` para no tocarlo |
| El adjunto no llega | Ejecuta `npm run calibrate`; si falla, usa `--no-attach` |
| Todo denegado por política | Revisa `sandbox.roots` en `config/default.json` |
| `EAMBIGUOUS` al editar | El ancla aparece varias veces: el modelo debe ampliarla. Es correcto que falle |
| El chat acumula contexto y se ralentiza | Empieza un hilo nuevo: es el reinicio más barato |

---

## Diagnóstico general

```bash
npm run doctor              # entorno, MCP, sandbox y reglas de seguridad
node src/cli.mjs run --task "..." --verbose
cat .audit/session-*.jsonl  # cada decisión, llamada y aprobación
```

Para ver qué está pasando en el navegador, asegúrate de `"headless": false` en la config
y observa la ventana mientras corre.

## Cuando Microsoft cambie el DOM

Se romperá; es inherente al enfoque. Todos los localizadores están en
**`src/driver/selectors.mjs`** y en ningún otro sitio. Inspecciona el elemento, prefiere
`aria-label` / `data-testid` / roles ARIA, y **nunca uses clases CSS** (están ofuscadas y rotan).
`npm test` seguirá pasando porque no depende del navegador.
