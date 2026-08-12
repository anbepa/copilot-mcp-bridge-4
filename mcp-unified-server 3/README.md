# 🧩 MCP Unified Server — Filesystem + Terminal + Browser + API Testing (transporte SSE)

Servidor **Model Context Protocol** listo para producción que unifica cuatro
familias de capacidades en un único endpoint HTTP:

| Grupo | Nº | Herramientas |
|---|---|---|
| **Filesystem (CRUD)** | 8 | `read_file`, `write_file`, `create_directory`, `list_directory`, `move_file`, `search_nodes` + extras `delete_node`, `get_file_info` |
| **Bash / Terminal** | 6 | `run`, `run_background`, `list_background`, `kill_background` + extras `get_background_output`, `get_system_info` |
| **Browser (Playwright)** | 59 | Navegación, snapshot de accesibilidad, clic/escritura/formularios, pestañas, red, almacenamiento, DevTools y control por coordenadas — `browser_*` |
| **API Testing & QA** | 9 | `set_api_auth`, `set_session_variable`, `build_and_send_request`, `validate_api_response`, `validate_json_schema`, `extract_response_data`, `run_postman_collection`, `generate_test_report` + extra `get_api_session` |

**82 herramientas** en total, expuestas por **SSE (Server-Sent Events)** y por
**Streamable HTTP**, y publicadas en Internet automáticamente mediante
**Cloudflare Tunnel** — listo para consumirse desde **Copilot Studio**, Claude
Desktop, VS Code, n8n, LangChain, etc.

> El grupo `browser_*` es un **port a Python del catálogo de Playwright MCP**.
> Playwright es una dependencia **opcional**: el servidor arranca sin ella y las
> tools se siguen publicando, devolviendo un error accionable con las
> instrucciones exactas de instalación. Arranca con `ENABLE_BROWSER=true ./start.sh`
> para instalarlo y descargar el navegador automáticamente.

> El grupo de **API Testing & QA** (estilo Postman + Serenity REST) **no añade
> ninguna dependencia opcional**: el motor **JSONPath** y el validador **JSON
> Schema Draft-07** están implementados dentro del propio proyecto. `newman`
> sólo es necesario si quieres el runner oficial de colecciones Postman; si no
> está, se usa un runner nativo incluido.

---

## 🚀 Arranque en un solo paso

### Linux / macOS / WSL
```bash
chmod +x start.sh
./start.sh
```

### Windows
```bat
start.bat
```

### Con automatización de navegador incluida
```bash
ENABLE_BROWSER=true ./start.sh          # Linux/macOS/WSL
```
```bat
set ENABLE_BROWSER=true && start.bat    :: Windows
```

El script realiza **todo** automáticamente:

1. Detecta Python 3.9+ y crea el entorno virtual `.venv`.
2. Instala las dependencias (`fastapi`, `uvicorn`).
3. **Si `ENABLE_BROWSER=true`**: instala `playwright` y descarga el navegador
   (`MCP_BROWSER_ENGINE`, por defecto `chromium`, ~150 MB la primera vez).
4. Valida la sintaxis del proyecto (`compileall`).
5. Verifica que el puerto esté libre (si no, asigna uno automáticamente).
6. Levanta el servidor MCP y espera a que `/health` responda.
7. Descarga `cloudflared` si no está instalado y abre un **Quick Tunnel**.
8. Imprime en consola la **URL pública final**:

```
=============================================================
   ✅  SERVIDOR MCP EN LÍNEA
=============================================================
  Local
    SSE            : http://127.0.0.1:8787/sse
    Streamable HTTP: http://127.0.0.1:8787/mcp
    Health         : http://127.0.0.1:8787/health

  URL PÚBLICA (Cloudflare Tunnel)
    Base           : https://xxxx-yyyy-zzzz.trycloudflare.com
    MCP SSE  →  https://xxxx-yyyy-zzzz.trycloudflare.com/sse
    MCP Stream →  https://xxxx-yyyy-zzzz.trycloudflare.com/mcp
=============================================================
```

`Ctrl+C` detiene el servidor y el túnel de forma ordenada.

---

## 🧭 Arquitectura de carpetas

```
mcp-unified-server/
│
├── start.sh                  # ⭐ Arranque automático Linux/macOS/WSL (servidor + túnel)
├── start.bat                 # ⭐ Arranque automático Windows (servidor + túnel)
├── main.py                   # Entrypoint: carga .env, CLI y arranca uvicorn
├── requirements.txt          # Dependencias base (fastapi, uvicorn, httpx)
├── requirements-browser.txt  # Dependencia OPCIONAL del grupo browser_* (playwright)
├── .env.example              # Plantilla de configuración (se copia a .env)
├── README.md                 # Esta guía
├── mcp-client-config.json    # Snippets de configuración para clientes MCP
│
├── server/                   # 📦 Código de la aplicación
│   ├── app.py                #   FastAPI: rutas /sse, /messages, /mcp, /health, /tools
│   ├── config.py             #   Settings tipados leídos de variables de entorno
│   │
│   ├── core/                 #   🧠 Núcleo independiente del transporte
│   │   ├── protocol.py       #     Motor JSON-RPC 2.0 + métodos MCP (initialize,
│   │   │                     #     tools/list, tools/call, ping, resources, prompts)
│   │   ├── registry.py       #     Registro de tools con decorador + validación de schema
│   │   └── security.py       #     Sandbox de rutas, denylist de comandos, truncado
│   │
│   ├── transport/            #   🔌 Capa de transporte
│   │   └── sse.py            #     Sesiones SSE, frames event/data, keep-alive
│   │
│   ├── browser/              #   🌐 Capa de sesión del navegador (Playwright)
│   │   └── session.py        #     Ciclo de vida browser/context/page, import diferido de
│   │                         #     Playwright, listeners (consola, red, diálogos), sistema
│   │                         #     de referencias `ref=eN` y JS de snapshot/highlight
│   │
│   ├── api/                  #   🧪 Motores del módulo de API Testing (sin dependencias)
│   │   ├── jsonpath.py       #     Evaluador JSONPath propio ($.a.b[0], [*], .., filtros)
│   │   ├── schema.py         #     Validador JSON Schema Draft-07 implementado a mano
│   │   ├── state.py          #     Sesión QA: auth, variables, historial, aserciones
│   │   └── postman.py        #     Parser de colecciones v2.1 + shim Node para scripts pm.*
│   │
│   └── tools/                #   🛠️ Herramientas expuestas al modelo
│       ├── filesystem.py     #     CRUD de archivos y búsqueda recursiva          (8 tools)
│       ├── terminal.py       #     Ejecución síncrona y procesos en background    (6 tools)
│       ├── browser.py        #     Catálogo completo browser_* portado de Playwright MCP (59)
│       └── apitesting.py     #     API Testing & QA estilo Postman/Serenity REST  (9 tools)
│
├── tests/
│   ├── test_smoke.py         # Suite E2E: levanta el server y valida las 82 tools (97 checks)
│   ├── test_browser_tools.py # Suite en proceso del grupo browser_* con doble de
│   │                         # prueba (FakePage/FakeContext/FakeLocator) — 141 checks
│   └── test_api_tools.py     # Suite del módulo de API Testing contra un servidor HTTP
│                             # real levantado en loopback — 207 checks
│
├── bin/                      # (autogenerado) binario cloudflared descargado
├── logs/                     # (autogenerado) server.log y cloudflared.log
├── browser-output/           # (autogenerado) capturas, trazas, vídeos y storage-state
└── reports/                  # (autogenerado) informes BDD de generate_test_report
```

### Principio de diseño

El proyecto separa **protocolo**, **transporte** y **herramientas**:

- `core/protocol.py` no sabe nada de HTTP: recibe un dict JSON-RPC y devuelve
  otro dict. Esto permite reutilizarlo sobre STDIO, WebSocket o SSE sin cambios.
- `transport/sse.py` sólo gestiona sesiones y el formato de frames SSE.
- `tools/*.py` declaran herramientas con el decorador `@registry.tool(...)`;
  añadir una nueva herramienta es escribir una función y decorarla — se publica
  sola en `tools/list`.
- `core/security.py` centraliza la política de seguridad (sandbox de rutas,
  denylist de comandos, límites de salida) para que ninguna tool la esquive.

---

## 🔌 Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Ficha del servidor: nombre, versión, transportes, tools |
| `GET` | `/health` | Healthcheck (`{"status":"ok"}`) usado por el script de arranque |
| `GET` | `/sse` | **Stream SSE**: emite `event: endpoint` con la URL de mensajes |
| `POST` | `/messages?sessionId=<id>` | Canal de entrada JSON-RPC de la sesión SSE (responde `202`) |
| `POST` | `/mcp` | **Streamable HTTP**: request/response JSON en una sola llamada |
| `GET` | `/mcp` | Stream SSE alternativo sobre la misma ruta |
| `GET` | `/tools` | Catálogo legible de herramientas (debug) |
| `GET` | `/docs` | Swagger UI autogenerado |

### Handshake SSE (spec 2024-11-05)

```
Cliente  ──GET /sse──────────────────────────────▶  Servidor
Cliente  ◀── event: endpoint                        (data: /messages?sessionId=ab12)
Cliente  ──POST /messages?sessionId=ab12 {jsonrpc}▶  Servidor → 202 Accepted
Cliente  ◀── event: message  {"jsonrpc":"2.0",...}  (por el stream abierto)
```

---

## 🛠️ Catálogo de herramientas

### 1. Filesystem

| Tool | Parámetros | Descripción |
|---|---|---|
| `read_file` | `path` *(string, req.)* | Lee el contenido completo del archivo en UTF-8 |
| `write_file` | `path` *(req.)*, `content` *(req.)*, `append` *(bool)* | Crea o sobrescribe un archivo; crea directorios padre |
| `create_directory` | `path` *(req.)* | Crea el directorio (idempotente, con padres) |
| `list_directory` | `path` *(req.)* | Lista entradas con tipo `[DIR]`/`[FILE]`, tamaño y fecha |
| `move_file` | `source` *(req.)*, `destination` *(req.)*, `overwrite` *(bool)* | Mueve o renombra archivos/directorios |
| `search_nodes` | `path` *(req.)*, `query` *(req.)*, `search_content` *(bool)*, `max_results` *(int)* | Búsqueda recursiva por nombre (glob o subcadena) y opcionalmente por contenido |
| `delete_node` ⭐ | `path` *(req.)*, `recursive` *(bool)* | Elimina archivo o directorio |
| `get_file_info` ⭐ | `path` *(req.)* | Metadatos: tamaño, fechas, permisos, symlink |

### 2. Terminal

| Tool | Parámetros | Descripción |
|---|---|---|
| `run` | `command` *(req.)*, `cwd`, `timeout` *(int)* | Ejecuta síncronamente; devuelve `stdout`, `stderr`, `exitCode`, `durationSeconds` |
| `run_background` | `command` *(req.)*, `cwd` | Lanza el proceso en segundo plano y devuelve `processId` |
| `list_background` | *(sin parámetros)* | Lista los procesos activos con PID, estado y uptime |
| `kill_background` | `processId` *(req.)* | Mata el proceso y todo su árbol de hijos (SIGKILL / `taskkill /T /F`) |
| `get_background_output` ⭐ | `processId` *(req.)*, `tail_lines` *(int)* | Salida acumulada del proceso en background |
| `get_system_info` ⭐ | *(sin parámetros)* | SO, versión de Python, workspace, política de seguridad, tools |

### 3. Browser (Playwright) — 59 tools

Port a Python del catálogo de **Playwright MCP**. El flujo recomendado es
*snapshot-first*: llama a `browser_snapshot` (o `browser_find`, mucho más barato
en tokens), toma la referencia `eN` del elemento y úsala como `target` en las
tools de acción. `target` acepta `e12`, `ref=e12` o cualquier selector de
Playwright (CSS, `text=`, `xpath=`).

#### 3.1 Automatización principal (23)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_navigate` | `url` *(req.)* | Navega a una URL; arranca el navegador si hace falta |
| `browser_navigate_back` | — | Vuelve atrás en el historial |
| `browser_close` | — | Cierra la página, el contexto y el navegador |
| `browser_snapshot` | `target`, `filename`, `depth` *(int)*, `boxes` *(bool)* | Árbol de accesibilidad con refs `eN`; puede guardarse a archivo |
| `browser_find` | `text` **XOR** `regex`, `context` *(int)* | Busca en el snapshot y devuelve sólo los nodos coincidentes con su ref. Soporta `/patrón/i` |
| `browser_click` | `target` *(req.)*, `element`, `doubleClick` *(bool)*, `button`, `modifiers` *(array)* | Clic simple/doble, botón izq/der/medio, con modificadores |
| `browser_hover` | `target` *(req.)*, `element` | Sitúa el puntero sobre el elemento |
| `browser_type` | `target` *(req.)*, `text` *(req.)*, `element`, `submit` *(bool)*, `slowly` *(bool)* | Escribe (fill o carácter a carácter) y opcionalmente pulsa Enter |
| `browser_fill_form` | `fields` *(array, req.)* | Rellena varios campos de una vez: `textbox`, `checkbox`, `radio`, `combobox`, `slider` |
| `browser_select_option` | `target` *(req.)*, `values` *(array, req.)*, `element` | Selecciona una o varias opciones de un `<select>` |
| `browser_press_key` | `key` *(req.)* | Pulsa una tecla (`Enter`, `ArrowLeft`, `a`…) |
| `browser_drag` | `startTarget` *(req.)*, `endTarget` *(req.)*, `startElement`, `endElement` | Arrastra de un elemento a otro |
| `browser_drop` | `target` *(req.)*, `paths` *(array)* **o** `data` *(object MIME→valor)* | Suelta archivos o datos sobre un elemento |
| `browser_file_upload` | `paths` *(array)*, `target` | Sube archivos al input; sin `paths` cancela el diálogo |
| `browser_handle_dialog` | `accept` *(bool, req.)*, `promptText` | Acepta o descarta el `alert`/`confirm`/`prompt` pendiente |
| `browser_evaluate` | `function` *(req.)*, `target`, `element` | Evalúa JS en la página o sobre un elemento |
| `browser_run_code_unsafe` 🔒 | `code` **o** `filename` | Ejecuta un snippet Python async con `page`/`context`/`session`. **Desactivado por defecto** |
| `browser_wait_for` | `time` *(number)*, `text`, `textGone` | Espera a que aparezca/desaparezca texto o pase un tiempo |
| `browser_resize` | `width` *(int, req.)*, `height` *(int, req.)* | Cambia el viewport |
| `browser_take_screenshot` | `target`, `element`, `type` (`png`\|`jpeg`), `filename`, `fullPage` *(bool)* | Captura de página o elemento. `fullPage` y `target` son excluyentes |
| `browser_console_messages` | `level`, `all` *(bool)*, `filename` | Mensajes de consola filtrados por nivel |
| `browser_network_requests` | `static` *(bool)*, `filter` *(regex)*, `filename` | Listado numerado de peticiones (oculta estáticos por defecto) |
| `browser_network_request` | `index` *(int, req.)*, `part` (`request`\|`response`\|`headers`\|`body`), `filename` | Detalle completo de una petición |

#### 3.2 Pestañas (1)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_tabs` | `action` *(req.: `list`\|`new`\|`close`\|`select`)*, `index` *(int)*, `url` | Lista, crea, cierra o selecciona pestañas |

#### 3.3 Configuración e instalación (2)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_get_config` | — | Configuración efectiva: si Playwright está instalado y su versión, motor, headless, viewport, timeout, `waitUntil`, user-agent, directorio de salida, si el navegador está abierto |
| `browser_install` | `engine` (`chromium`\|`firefox`\|`webkit`) | Descarga el binario del navegador (timeout 900 s) |

#### 3.4 Red y mocking (4)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_network_state_set` | `state` *(req.: `offline`\|`online`)* | Simula pérdida de conexión |
| `browser_route` | `pattern` *(req.)*, `status` *(int)*, `body`, `contentType`, `headers`, `abort` *(bool)* | Intercepta y responde/aborta peticiones |
| `browser_route_list` | — | Lista las intercepciones activas |
| `browser_unroute` | `pattern` | Elimina una intercepción (o todas si se omite) |

#### 3.5 Almacenamiento (17)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_cookie_list` | `domain`, `path` | Lista cookies, con filtros opcionales |
| `browser_cookie_get` | `name` *(req.)* | Obtiene una cookie concreta |
| `browser_cookie_set` | `name` *(req.)*, `value` *(req.)*, `domain`, `path`, `expires` *(number)*, `httpOnly` *(bool)*, `secure` *(bool)*, `sameSite` (`Strict`\|`Lax`\|`None`) | Crea una cookie |
| `browser_cookie_delete` | `name` *(req.)* | Elimina una cookie |
| `browser_cookie_clear` | — | Borra todas las cookies |
| `browser_localstorage_list` / `browser_sessionstorage_list` | — | Todos los pares clave-valor |
| `browser_localstorage_get` / `browser_sessionstorage_get` | `key` *(req.)* | Lee una clave |
| `browser_localstorage_set` / `browser_sessionstorage_set` | `key` *(req.)*, `value` *(req.)* | Escribe una clave |
| `browser_localstorage_delete` / `browser_sessionstorage_delete` | `key` *(req.)* | Elimina una clave |
| `browser_localstorage_clear` / `browser_sessionstorage_clear` | — | Vacía el almacén |
| `browser_storage_state` | `filename` | Guarda cookies + localStorage a un JSON reutilizable |
| `browser_set_storage_state` | `filename` *(req.)* | Restaura una sesión desde ese JSON |

#### 3.6 DevTools (6)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_highlight` | `target` *(req.)*, `element`, `style` | Dibuja un recuadro persistente sobre un elemento |
| `browser_hide_highlight` | `target`, `element` | Quita un resaltado (o todos) |
| `browser_start_tracing` | — | Inicia un trace de Playwright (capturas + snapshots) |
| `browser_stop_tracing` | `filename` | Guarda el `.zip` — ábrelo en <https://trace.playwright.dev> |
| `browser_start_video` | `filename`, `size` *(object)* | Inicia grabación de vídeo. ⚠️ Recrea el contexto: vuelve a navegar después |
| `browser_stop_video` | — | Detiene la grabación y devuelve la ruta del `.webm` |

#### 3.7 Control por coordenadas / visión (6)

| Tool | Parámetros | Descripción |
|---|---|---|
| `browser_mouse_move_xy` | `x` *(req.)*, `y` *(req.)* | Mueve el puntero |
| `browser_mouse_click_xy` | `x` *(req.)*, `y` *(req.)*, `button`, `clickCount` *(int)*, `delay` *(number)* | Clic en coordenadas |
| `browser_mouse_down` | `button` | Pulsa y mantiene un botón |
| `browser_mouse_up` | `button` | Suelta el botón |
| `browser_mouse_drag_xy` | `startX`, `startY`, `endX`, `endY` *(todos req.)* | Arrastre por coordenadas |
| `browser_mouse_wheel` | `deltaX` *(req.)*, `deltaY` *(req.)* | Scroll con la rueda |

⭐ = herramienta adicional añadida para robustecer el control del entorno.
🔒 = requiere activación explícita por seguridad.

#### Diferencias respecto al Playwright MCP original

- **Sistema de referencias propio**: en lugar del `aria-ref` interno (API privada
  e inestable), `browser_snapshot` inyecta `data-mcp-ref="eN"` en el DOM y
  construye el árbol de roles/nombres. El resultado es equivalente y estable.
- `browser_run_code_unsafe` es el análogo Python de `browser_run_code_unsafe`
  (JS): ejecuta un snippet **async de Python** con `page`, `context` y `session`
  en el scope. Está cerrado por defecto (`MCP_ENABLE_UNSAFE_BROWSER_CODE`).
- **No portadas** (5): `browser_annotate`, `browser_resume`, `browser_video_chapter`,
  `browser_video_show_actions`, `browser_video_hide_actions`. Dependen de la UI
  del *Playwright Dashboard* (Node) y no tienen equivalente en la API pública de
  Playwright para Python.

---

### 4. API Testing & QA (estilo Postman / Serenity REST) — 9 tools

Módulo de pruebas de APIs con **estado de sesión**: credenciales, variables,
historial de peticiones y registro de aserciones se mantienen entre llamadas,
de forma que el modelo puede encadenar `set_api_auth → build_and_send_request →
validate_api_response → extract_response_data → generate_test_report` como un
escenario BDD completo.

| Tool | Parámetros | Descripción |
|---|---|---|
| `set_api_auth` | `type` *(req.:* `bearer`\|`basic`\|`apiKey`\|`oauth2`\|`none`*)*, `token`, `username`, `password`, `headerName`, `headerPrefix`, `tokenUrl`, `clientId`, `clientSecret`, `scope`, `audience` | Configura las credenciales globales de la sesión. Con `oauth2` + `tokenUrl` hace el *client credentials grant* y guarda el `access_token` automáticamente. El token/password nunca se devuelve en claro (se enmascara). |
| `set_session_variable` | `key` *(req.)*, `value` *(any)*, `secret` ⭐, `delete` ⭐ | Define/actualiza variables de sesión (equivalente a `pm.environment.set`). Se interpolan con `{{key}}` en URL, headers, query y body. Marcadas como `secret` se redactan en informes. |
| `build_and_send_request` | `method` *(req.)*, `url` *(req.)*, `headers`, `queryParams`, `bodyType` (`json`\|`form-data`\|`x-www-form-urlencoded`\|`raw`\|`none`), `body`, `files` *(`[{fieldName, filePath}]`)*, `timeoutMs` ⭐, `followRedirects` ⭐, `verifyTls` ⭐, `name` ⭐ | Ejecuta la petición HTTP completa. **Retorna** `statusCode`, `responseTimeMs`, `responseHeaders`, `responseBody` (parseado si es JSON) + `requestIndex`, `sizeBytes` y `url` final. |
| `validate_api_response` | `expectedStatus`, `maxResponseTimeMs`, `requiredFields` *(array)*, `valueAssertions` *(`[{jsonPath, operator, expected}]`)*, `expectedHeaders` ⭐, `bodyContains` ⭐, `requestIndex` ⭐, `failFast` ⭐ | Motor de aserciones sobre la última respuesta (o la indicada). Devuelve el detalle aserción a aserción y `passed`/`failed`. |
| `validate_json_schema` | `schema` *(objeto o string)*, `schemaPath` ⭐, `jsonPath` ⭐, `requestIndex` ⭐ | Valida el contrato estructural contra **JSON Schema Draft-07**. Los errores indican la ruta con notación JSONPath (`$.data.total`). |
| `extract_response_data` | `jsonPath`, `variableName` *(req.)*, `regex` ⭐, `header` ⭐, `all` ⭐, `defaultValue` ⭐, `requestIndex` ⭐, `secret` ⭐ | Extrae valores de la respuesta previa por JSONPath, regex o cabecera y los guarda como variable de sesión reutilizable con `{{...}}`. |
| `run_postman_collection` | `collectionPath` *(req.)*, `environmentPath`, `iterationData`, `runner` ⭐, `folder` ⭐, `bail` ⭐, `delayMs` ⭐ | Ejecuta una colección Postman v2.1 completa y devuelve el consolidado de aserciones, tiempos y fallos. |
| `generate_test_report` | `suiteName`, `includeResponseBody`, `environment`, `outputPath` ⭐, `reset` ⭐ | Compila historial, SLA, validaciones de esquema y aserciones en un **informe ejecutivo Markdown con formato BDD (Given/When/Then)**. Lo devuelve y, si das `outputPath`, lo escribe en disco. |
| `get_api_session` ⭐ | *(sin parámetros)* | Introspección del estado: auth enmascarada, variables, resumen del historial, contadores de aserciones y avisos. |

**Operadores soportados en `valueAssertions` (16):** `equals`, `notEquals`,
`contains`, `notContains`, `notNull`, `isNull`, `greaterThan`, `lessThan`,
`greaterOrEqual`, `lessOrEqual`, `matches` (regex), `in`, `type`, `length`,
`empty`, `notEmpty`.

**Runner de colecciones en 3 niveles** (el campo `runner` de la respuesta indica
siempre el modo realmente usado):

1. `newman` — si está en el `PATH` (`npm install -g newman`). Es el runner
   oficial de Postman; se invoca con `--reporters json`. → `runner: "newman"`
2. `native` — si hay **Node.js**: Python hace las peticiones HTTP y Node evalúa
   los scripts `pm.test`/`pm.expect`/`pm.environment` de la colección. → `runner: "native"`
3. `python` — sin Node: ejecuta las peticiones aplicando la aserción implícita
   de estado 2xx; los scripts se reportan como omitidos. → `runner: "python"`

**Seguridad del módulo:** las cabeceras sensibles (`authorization`, `cookie`,
`x-api-key`…) se **redactan** en el historial y en los informes; puedes restringir
los destinos con `MCP_API_HOST_ALLOWLIST` (regex separadas por coma) y desactivar
todo el grupo con `MCP_ENABLE_API_TESTING=false`.

---

## 🤖 Conexión desde Copilot Studio

1. Ejecuta `./start.sh` y copia la **URL pública** (`https://....trycloudflare.com`).
2. En Copilot Studio → **Tools / Add a tool → Model Context Protocol**.
3. Configura:
   - **Server URL**: `https://<tu-url>.trycloudflare.com/sse`
   - **Transport**: `Server-Sent Events (SSE)`
   - **Authentication**: `None` (o `API Key / Bearer` si defines `MCP_AUTH_TOKEN`).
4. Guarda: Copilot Studio hará el `initialize` + `tools/list` y descubrirá las 82 herramientas.

> ⚠️ La URL de un Quick Tunnel es **efímera**: cambia en cada arranque. Para una
> URL fija define `CLOUDFLARED_TUNNEL_TOKEN` en `.env` con el token de un túnel
> permanente creado en *Cloudflare Zero Trust → Networks → Tunnels*.

### Otros clientes (Claude Desktop, VS Code, Cursor)

```json
{
  "mcpServers": {
    "unified-fs-bash": {
      "type": "sse",
      "url": "https://<tu-url>.trycloudflare.com/sse"
    }
  }
}
```

---

## ⚙️ Configuración (`.env`)

| Variable | Def. | Descripción |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` | Host de escucha |
| `MCP_PORT` | `8787` | Puerto local |
| `MCP_LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error` |
| `ENABLE_TUNNEL` | `true` | Levanta cloudflared automáticamente |
| `CLOUDFLARED_TUNNEL_TOKEN` | *(vacío)* | Token de túnel permanente (URL fija) |
| `CLOUDFLARED_HOSTNAME` | *(vacío)* | Hostname del túnel permanente (informativo) |
| `MCP_AUTH_TOKEN` | *(vacío)* | Si se define, exige `Authorization: Bearer <token>` |
| `MCP_WORKSPACE_ROOT` | *cwd* | Raíz permitida para el filesystem |
| `MCP_ALLOW_OUTSIDE_ROOT` | `false` | Permite salir de la raíz (⚠️ peligroso) |
| `MCP_ENABLE_TERMINAL` | `true` | Habilita/deshabilita el grupo de terminal |
| `MCP_COMMAND_DENYLIST` | *(vacío)* | Patrones regex de comandos prohibidos, separados por coma |
| `ENABLE_BROWSER` | `false` | Que `start.sh`/`start.bat` instale Playwright + navegador al arrancar |
| `MCP_ENABLE_BROWSER` | `true` | Habilita/deshabilita el grupo completo `browser_*` |
| `MCP_BROWSER_ENGINE` | `chromium` | `chromium` \| `firefox` \| `webkit` |
| `MCP_BROWSER_HEADLESS` | `true` | Ejecuta el navegador sin interfaz |
| `MCP_BROWSER_WIDTH` | `1280` | Ancho del viewport |
| `MCP_BROWSER_HEIGHT` | `720` | Alto del viewport |
| `MCP_BROWSER_TIMEOUT_MS` | `30000` | Timeout por defecto de las acciones |
| `MCP_BROWSER_WAIT_UNTIL` | `domcontentloaded` | `load` \| `domcontentloaded` \| `networkidle` \| `commit` |
| `MCP_BROWSER_USER_AGENT` | *(vacío)* | User-Agent personalizado |
| `MCP_BROWSER_EXECUTABLE_PATH` | *(vacío)* | Ruta a un binario de navegador propio |
| `MCP_BROWSER_OUTPUT_DIR` | `<workspace>/browser-output` | Capturas, trazas, vídeos y storage-state |
| `MCP_ENABLE_UNSAFE_BROWSER_CODE` | `false` | ⚠️ Habilita `browser_run_code_unsafe` (equivale a RCE) |
| `MCP_ENABLE_API_TESTING` | `true` | Habilita/deshabilita el grupo completo de API Testing |
| `MCP_API_BASE_URL` | *(vacío)* | Prefijo para URLs relativas (`/v1/clientes`) |
| `MCP_API_TIMEOUT` | `30` | Timeout (s) por petición HTTP |
| `MCP_API_VERIFY_TLS` | `true` | Verificación de certificados TLS (`false` sólo en QA) |
| `MCP_API_FOLLOW_REDIRECTS` | `true` | Seguir redirecciones 3xx |
| `MCP_API_MAX_REDIRECTS` | `10` | Máximo de saltos de redirección |
| `MCP_API_HOST_ALLOWLIST` | *(vacío)* | Regex de hosts permitidos, separadas por coma. Vacío = cualquiera |
| `MCP_API_MAX_BODY_CHARS` | `20000` | Truncado del cuerpo de respuesta devuelto/almacenado |
| `MCP_API_MAX_HISTORY` | `500` | Peticiones conservadas en el historial de sesión |
| `MCP_API_SCRIPT_TIMEOUT` | `30` | Timeout (s) de cada script `pm.*` evaluado en Node |
| `MCP_API_COLLECTION_TIMEOUT` | `900` | Timeout (s) total de una colección Postman |
| `MCP_API_REPORT_DIR` | `<workspace>/reports` | Destino de `generate_test_report` |
| `MCP_COMMAND_TIMEOUT` | `120` | Timeout (s) de `run` |
| `MCP_MAX_OUTPUT_CHARS` | `200000` | Truncado de salidas |
| `MCP_MAX_READ_BYTES` | `5000000` | Tamaño máximo de `read_file` |
| `MCP_MAX_SEARCH_RESULTS` | `500` | Límite de `search_nodes` |
| `MCP_SSE_KEEPALIVE` | `15` | Segundos entre comentarios keep-alive del stream |

---

## 🔒 Seguridad

Este servidor **ejecuta comandos y modifica archivos** en la máquina donde corre.
Recomendaciones al exponerlo por Internet:

1. **Define siempre `MCP_AUTH_TOKEN`** cuando el túnel esté activo.
2. **Acota `MCP_WORKSPACE_ROOT`** a un directorio de trabajo concreto; el
   sandbox bloquea `../` y symlinks que escapen de esa raíz (probado en la suite).
3. Usa `MCP_COMMAND_DENYLIST` para bloquear comandos destructivos, p. ej.:
   `MCP_COMMAND_DENYLIST=rm\s+-rf\s+/,mkfs,shutdown,reboot,dd\s+if=`
4. Pon `MCP_ENABLE_TERMINAL=false` si sólo necesitas las capacidades de archivos.
5. Pon `MCP_ENABLE_BROWSER=false` si no necesitas automatización web.
6. **Nunca actives `MCP_ENABLE_UNSAFE_BROWSER_CODE=true` en un servidor
   expuesto**: `browser_run_code_unsafe` ejecuta código Python arbitrario con los
   permisos del proceso. Por eso viene cerrado de fábrica.
7. Restringe los destinos de las pruebas de API con `MCP_API_HOST_ALLOWLIST`
   (p. ej. `^[a-z0-9.-]+\.qa\.interno$`) para que el servidor no pueda usarse
   como proxy hacia servicios internos arbitrarios. Mantén `MCP_API_VERIFY_TLS=true`.
8. Para producción real, sustituye el Quick Tunnel por un túnel permanente con
   políticas de **Cloudflare Access** delante.

---

## ✅ Validación

```bash
python tests/test_smoke.py           # E2E contra el servidor real  →  97 checks
python tests/test_browser_tools.py   # Grupo browser_* en proceso   → 141 checks
python tests/test_api_tools.py       # Grupo API Testing & QA       → 207 checks
```

**`test_smoke.py`** arranca el servidor real en un puerto libre y un workspace
temporal y ejecuta **97 verificaciones**: handshake MCP, transporte SSE completo
(`GET /sse` → `event: endpoint` → `POST /messages` → `event: message`),
Streamable HTTP, las 82 herramientas publicadas, validez de todos los
`inputSchema`, casos de error, timeouts, el sandbox de rutas y un escenario
end-to-end de API testing que usa el propio `/health` del servidor como API bajo
prueba.

**`test_browser_tools.py`** inyecta un doble de prueba (`FakePage`,
`FakeContext`, `FakeLocator`) en la sesión del navegador y ejecuta **141
verificaciones** sobre el grupo `browser_*` sin necesidad de descargar
Chromium: mapeo de argumentos a la API de Playwright, resolución de referencias
(`e12` → `[data-mcp-ref="e12"]`), filtrado del snapshot, parseo de `/regex/i`,
aislamiento local/sessionStorage, y todos los caminos de error y guardas de
seguridad.

**`test_api_tools.py`** levanta un **servidor HTTP real** en loopback (con rutas
`/users`, `/users/{id}`, `/status?code=`, `/slow?ms=`, `/whoami`, `/echo`,
`/token`, `/text`, `/redirect`) y ejecuta **207 verificaciones** sobre tráfico
HTTP auténtico: los 4 tipos de auth, interpolación de variables, los 5
`bodyType` (incluida subida de archivos multipart), los 16 operadores de
aserción, el validador JSON Schema, extracción por JSONPath/regex/cabecera, el
runner de colecciones en sus tres modos y el informe BDD.

```
============================================================
  ✅  97/97 PRUEBAS SUPERADAS
============================================================
TODO OK — 141/141 comprobaciones superadas.
TODO OK — 207/207 comprobaciones superadas.
```

> **Nota de alcance:** el entorno de construcción no tenía red para descargar
> los binarios de Chromium, por lo que las rutas que hablan con un navegador
> **real** se validaron contra el doble de prueba, no contra Chromium. Ejecuta
> `ENABLE_BROWSER=true ./start.sh` y luego `browser_navigate` +
> `browser_snapshot` para confirmarlo en tu máquina en menos de un minuto.
>
> Por el mismo motivo, el módulo de API testing se validó contra un servidor
> HTTP real **en loopback**, no contra APIs de Internet, y el runner `newman`
> se ejercitó con un doble (el runner `native` sobre Node sí se probó de
> verdad). Ambas rutas son idénticas en código; sólo cambia el destino.

---

## 🧪 Prueba manual rápida (curl)

```bash
# Listar herramientas por Streamable HTTP
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m json.tool

# Ejecutar un comando
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"run","arguments":{"command":"echo hola"}}}'

# Abrir el stream SSE
curl -N http://127.0.0.1:8787/sse

# --- Navegador: comprobar estado, navegar y sacar el snapshot ---------------
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"browser_get_config","arguments":{}}}' | python -m json.tool

curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
       "params":{"name":"browser_navigate","arguments":{"url":"https://example.com"}}}'

curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call",
       "params":{"name":"browser_snapshot","arguments":{}}}'

# --- API Testing: escenario BDD completo ------------------------------------
# 1) Credenciales de la sesión
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call",
       "params":{"name":"set_api_auth","arguments":{"type":"bearer","token":"mi-token"}}}'

# 2) Petición
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call",
       "params":{"name":"build_and_send_request","arguments":{
         "method":"GET","url":"http://127.0.0.1:8787/health"}}}' | python -m json.tool

# 3) Aserciones (estado + SLA + campos + valores)
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call",
       "params":{"name":"validate_api_response","arguments":{
         "expectedStatus":200,"maxResponseTimeMs":2000,
         "requiredFields":["status","tools"],
         "valueAssertions":[{"jsonPath":"$.status","operator":"equals","expected":"ok"},
                            {"jsonPath":"$.tools","operator":"greaterThan","expected":0}]}}}'

# 4) Extraer un valor a variable de sesión (reutilizable como {{totalTools}})
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":9,"method":"tools/call",
       "params":{"name":"extract_response_data","arguments":{
         "jsonPath":"$.tools","variableName":"totalTools"}}}'

# 5) Informe ejecutivo en Markdown con formato Given/When/Then
curl -s -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/call",
       "params":{"name":"generate_test_report","arguments":{
         "suiteName":"Smoke de salud","environment":"local",
         "includeResponseBody":true,"outputPath":"salud.md"}}}'
```

---

## 🧯 Solución de problemas

| Síntoma | Causa / solución |
|---|---|
| `No se pudo obtener cloudflared` | Sin acceso a GitHub. Instala cloudflared manualmente y vuelve a ejecutar; el servidor local sigue funcionando. |
| La URL pública no aparece | Revisa `logs/cloudflared.log`; los Quick Tunnels a veces tardan ~20 s. |
| `401 Token de autorización inválido` | Falta la cabecera `Authorization: Bearer <MCP_AUTH_TOKEN>`. |
| `Acceso denegado: ... fuera del workspace` | Ajusta `MCP_WORKSPACE_ROOT` o usa rutas relativas dentro de la raíz. |
| El puerto está ocupado | El script asigna uno libre automáticamente; también puedes cambiar `MCP_PORT`. |
| Copilot Studio no descubre tools | Verifica que la URL termine en `/sse` y que `curl -N <url>/sse` emita `event: endpoint`. |
| `Playwright no está instalado` | Ejecuta `ENABLE_BROWSER=true ./start.sh`, o bien `pip install -r requirements-browser.txt && python -m playwright install chromium`. También puedes llamar a la tool `browser_install`. |
| El navegador no arranca en Linux (faltan librerías) | `python -m playwright install-deps chromium` (necesita sudo) o instala `libnss3 libatk1.0-0 libgbm1 libasound2`. |
| `La referencia 'eN' ya no existe en la página` | El DOM cambió tras la última captura. Vuelve a llamar a `browser_snapshot` (o `browser_find`) y usa las refs nuevas. |
| `browser_run_code_unsafe está deshabilitado` | Es intencionado. Sólo en entornos de confianza: `MCP_ENABLE_UNSAFE_BROWSER_CODE=true`. |
| El vídeo sale vacío | `browser_start_video` recrea el contexto: vuelve a llamar a `browser_navigate` **después** de iniciarlo. |

---

**Licencia:** MIT · **Versión:** 2.0.0 (Filesystem + Terminal + Browser)
