# Cambios

## v0.4 — el verificador mentía

Tu tercera ejecución **funcionó** (4 turnos, 128 s, ambas ediciones aplicadas). Pero `login`
te dijo *"La sesión NO persistió"* y justo después `run` entró sin pedirte credenciales.
La sesión sí persistió: el que se equivocaba era el verificador.

### Falso negativo en la verificación de sesión

**Causa:** `run` espera hasta **60 s** a que aparezca el composer (`editorTimeoutMs`), pero
`verifySession` esperaba solo **25 s**. Tu M365 va lento — el primer turno tardó 62 s — así
que el chat no llegaba a tiempo y se daba la sesión por muerta. Encima, un `catch` vacío se
tragaba el motivo: era imposible distinguir "la sesión murió" de "tardó demasiado".

**Arreglado:**
- La verificación usa **el mismo timeout que `run`** (`editorTimeoutMs`, 60 s).
- Devuelve **tres estados**, no un booleano: `alive` / `expired` / `unknown`.
- Solo se declara caducada si **se ve la pantalla de login**. Si simplemente no dio tiempo,
  ahora dice *"no se pudo confirmar"* y **sale con código 0**, sugiriéndote ejecutar la tarea.
  Decirte que repitieras un MFA que no hacía falta era el peor efecto del bug anterior.
- Carrera explícita entre "aparece el chat" y "aparece el login", con la URL como segunda
  opinión, en vez de un único timeout ciego.
- Reintento con espera si Chromium aún no ha soltado el cerrojo del perfil (`SingletonLock`).
- Se quitó el mensaje contradictorio: antes decía *"Sesión verificada y guardada"* **antes**
  de verificarla de verdad. Ahora dice *"Chat detectado. Comprobando que la sesión persista…"*.

### Selección de modelo más útil

En tu log: `⚠ Modelo "GPT 5.6 Think" no disponible en el menú`. Ahora, si no lo encuentra:

- Reintenta comparando **normalizado** (ignora mayúsculas, espacios, guiones y puntos), así
  `GPT 5.6 Think` encuentra a `GPT-5.6 Think`.
- Si aun así no está, **te lista los modelos que sí tiene tu menú** y te da el comando exacto
  para fijar uno. Antes solo decía "no disponible" y te dejaba adivinando.

Esto importa más de lo que parece: al caer al modelo por defecto del tenant, el turno 1 de tu
log falló el formato y costó un turno extra de reparación (62 s).

### Pruebas

96 tests (antes 85). Los 11 nuevos cubren la matriz completa de decisión de sesión, incluido
el caso exacto de tu log: **ningún intento concluyente ⇒ `unknown`, jamás `expired`**.

---

## v0.3 — sesión que de verdad persiste + headless

Correcciones nacidas de tu segunda ejecución real. La ejecución en sí **funcionó perfecta**
(3 turnos, 75 s, ambas ediciones aplicadas con su diff aprobado), así que todo lo de esta
versión va sobre el único problema que quedaba: el login.

### La causa real de que te pidiera login otra vez

La pista estaba en el nombre de la carpeta: `copilot-mcp-bridge 2`. Habías descomprimido una
versión nueva del proyecto. Como el perfil del navegador vivía **dentro** del proyecto
(`./.browser-profile`), la carpeta nueva venía sin perfil. La sesión no se perdía: se buscaba
donde no estaba. Ningún arreglo del código de login podía resolver eso.

- **El perfil ahora vive en `~/.copilot-mcp-bridge/browser-profile`**, fuera del proyecto.
  Sobrevive a actualizaciones, a zips nuevos y a ejecutar desde otro directorio.
- **Migración automática**: si tienes un perfil antiguo dentro del proyecto, se copia solo al
  nuevo sitio. Nunca sobrescribe un perfil existente ni borra el antiguo.
- **`--profile <ruta>`** para usar otro perfil (p. ej. dos cuentas).

### Dos causas secundarias, también corregidas

- `login` hacía `process.exit(0)` justo tras cerrar el navegador. Chromium vuelca cookies y
  tokens MSAL **al apagarse**, y ese volcado quedaba truncado. Ahora el cierre es ordenado.
- Se daba la sesión por buena sin comprobarla. Ahora `login` cierra el navegador, lo **reabre**
  y verifica que la sesión sigue viva. Si no persiste, **sale con error** en vez de decirte
  que todo fue bien.

### Modo headless

`--headless` ya existía; ahora está documentado y es más robusto.

- `--headless` / `--headed` en `run`, más `CMB_HEADLESS=1`.
- Si lanzas `--headless` sin sesión válida, el puente **te lo dice claramente** en vez de
  esperar a que expire el tiempo con un error genérico. En headless no hay dónde resolver
  un MFA: el login se hace una vez con ventana.
- `login` y `session` **reintentan con ventana** si la verificación headless falla, para
  distinguir "la sesión murió" de "tu tenant bloquea headless". Un diagnóstico equivocado
  te haría repetir un MFA innecesario.

### Nuevo comando

```bash
node src/cli.mjs session    # ¿sigue viva la sesión? Sin lanzar ninguna tarea
```

### Pruebas

85 tests (antes 67). Los 18 nuevos cubren la resolución del perfil, la expansión de `~`, la
precedencia de flags sobre entorno y las nueve reglas de la migración.

---

## v0.2 — el bucle de `ENOMATCH`

- Copilot enviaba `{search, replace}` y el servidor exigía `{oldText, newText}`. Ahora se
  aceptan ambos, más otros alias.
- El mensaje de error decía "vuelve a leer el archivo" cuando el archivo estaba bien: eso
  convertía un fallo puntual en un bucle infinito hasta agotar los turnos. Ahora `EBADEDIT`
  (campos mal) se distingue de `ENOMATCH` (ancla ausente de verdad).
- El `hint` se perdía en el transporte MCP; ahora se propaga hasta Copilot.
- Coincidencia tolerante a diferencias de espacios e indentación.
- Cortacircuitos: si un paso falla dos veces igual, se detiene en vez de reintentar.

## v0.1 — versión inicial

Context Compiler, protocolo de bloques, ejecución paralela por oleadas, Policy Engine con
aprobación humana por diff, auditoría JSONL, driver Playwright y MockDriver para pruebas
sin navegador.
