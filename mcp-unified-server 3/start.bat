@echo off
REM ============================================================================
REM  MCP UNIFIED SERVER - Arranque automatico (Windows)
REM  1) Crea el venv e instala dependencias
REM  2) Levanta el servidor MCP local (FastAPI + SSE)
REM  3) Descarga/ejecuta cloudflared y publica la URL publica
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "BASE_DIR=%~dp0"
set "VENV_DIR=%BASE_DIR%.venv"
set "BIN_DIR=%BASE_DIR%bin"
set "LOG_DIR=%BASE_DIR%logs"
set "SERVER_LOG=%LOG_DIR%\server.log"
set "TUNNEL_LOG=%LOG_DIR%\cloudflared.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

echo.
echo =============================================================
echo   MCP UNIFIED SERVER - Filesystem+Terminal+Browser+API Testing - SSE
echo =============================================================

REM --- .env -----------------------------------------------------------------
if not exist "%BASE_DIR%.env" (
  if exist "%BASE_DIR%.env.example" copy /y "%BASE_DIR%.env.example" "%BASE_DIR%.env" >nul
)
set "MCP_HOST=127.0.0.1"
set "MCP_PORT=8787"
set "ENABLE_TUNNEL=true"
set "CLOUDFLARED_TUNNEL_TOKEN="
if exist "%BASE_DIR%.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%BASE_DIR%.env") do (
    if not "%%~A"=="" set "%%~A=%%~B"
  )
)

REM --- Python ---------------------------------------------------------------
set "PY="
where python >nul 2>&1 && set "PY=python"
if "%PY%"=="" ( where py >nul 2>&1 && set "PY=py -3" )
if "%PY%"=="" (
  echo [FAIL] Se requiere Python 3.9+ en el PATH. Descargalo en https://python.org
  pause & exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [ OK ]  Python detectado: %%v

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [INFO]  Creando entorno virtual...
  %PY% -m venv "%VENV_DIR%" || ( echo [FAIL] No se pudo crear el venv & pause & exit /b 1 )
)
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

"%VENV_PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo [INFO]  Instalando dependencias...
  "%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
  "%VENV_PY%" -m pip install -r "%BASE_DIR%requirements.txt" || (
    echo [FAIL] Fallo instalando dependencias & pause & exit /b 1 )
)
echo [ OK ]  Dependencias listas

REM --- Navegador opcional (Playwright) --------------------------------------
if /I "%ENABLE_BROWSER%"=="true" (
  "%VENV_PY%" -c "import playwright" >nul 2>&1
  if errorlevel 1 (
    echo [INFO]  Instalando Playwright ^(tools browser_*^)...
    "%VENV_PY%" -m pip install -r "%BASE_DIR%requirements-browser.txt" || echo [WARN]  No se pudo instalar Playwright
  ) else (
    echo [ OK ]  Playwright ya esta instalado
  )
  if not defined MCP_BROWSER_ENGINE set "MCP_BROWSER_ENGINE=chromium"
  "%VENV_PY%" -c "import playwright" >nul 2>&1
  if not errorlevel 1 (
    echo [INFO]  Descargando el navegador %MCP_BROWSER_ENGINE% ^(puede tardar la primera vez^)...
    "%VENV_PY%" -m playwright install %MCP_BROWSER_ENGINE% || echo [WARN]  Usa la tool browser_install mas tarde
    echo [ OK ]  Navegador %MCP_BROWSER_ENGINE% listo
  )
) else (
  "%VENV_PY%" -c "import playwright" >nul 2>&1
  if errorlevel 1 (
    echo [INFO]  Playwright no instalado. Para habilitar browser_*:  set ENABLE_BROWSER=true ^&^& start.bat
  ) else (
    echo [ OK ]  Playwright detectado: tools browser_* operativas
  )
)

REM --- Validacion de sintaxis ----------------------------------------------
"%VENV_PY%" -m compileall -q "%BASE_DIR%server" "%BASE_DIR%main.py" >nul 2>&1
if errorlevel 1 ( echo [WARN]  Advertencias en la compilacion ) else ( echo [ OK ]  Validacion de sintaxis correcta )

REM --- Arranque del servidor ------------------------------------------------
echo [INFO]  Levantando servidor MCP en http://%MCP_HOST%:%MCP_PORT% ...
start "MCP-Server" /min cmd /c ""%VENV_PY%" "%BASE_DIR%main.py" > "%SERVER_LOG%" 2>&1"

set "READY="
for /l %%i in (1,1,40) do (
  if not defined READY (
    "%VENV_PY%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%MCP_PORT%/health',timeout=2)" >nul 2>&1
    if not errorlevel 1 set "READY=1"
    if not defined READY timeout /t 1 /nobreak >nul
  )
)
if not defined READY (
  echo [FAIL] El servidor no respondio. Revisa "%SERVER_LOG%"
  type "%SERVER_LOG%"
  pause & exit /b 1
)
echo [ OK ]  Servidor MCP activo - log: %SERVER_LOG%

REM --- cloudflared ----------------------------------------------------------
set "PUBLIC_URL="
if /i "%ENABLE_TUNNEL%"=="true" (
  set "CF_BIN="
  where cloudflared >nul 2>&1 && set "CF_BIN=cloudflared"
  if "!CF_BIN!"=="" if exist "%BIN_DIR%\cloudflared.exe" set "CF_BIN=%BIN_DIR%\cloudflared.exe"
  if "!CF_BIN!"=="" (
    echo [INFO]  Descargando cloudflared...
    powershell -NoProfile -Command "try{Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%BIN_DIR%\cloudflared.exe' -UseBasicParsing}catch{exit 1}"
    if exist "%BIN_DIR%\cloudflared.exe" set "CF_BIN=%BIN_DIR%\cloudflared.exe"
  )

  if "!CF_BIN!"=="" (
    echo [WARN]  No se pudo obtener cloudflared. El servidor sigue disponible en local.
  ) else (
    echo [ OK ]  cloudflared: !CF_BIN!
    break > "%TUNNEL_LOG%"
    if not "%CLOUDFLARED_TUNNEL_TOKEN%"=="" (
      echo [INFO]  Iniciando tunel con token configurado...
      start "cloudflared" /min cmd /c ""!CF_BIN!" tunnel --no-autoupdate run --token %CLOUDFLARED_TUNNEL_TOKEN% > "%TUNNEL_LOG%" 2>&1"
      set "PUBLIC_URL=%CLOUDFLARED_HOSTNAME%"
    ) else (
      echo [INFO]  Iniciando Cloudflare Quick Tunnel...
      start "cloudflared" /min cmd /c ""!CF_BIN!" tunnel --no-autoupdate --url http://127.0.0.1:%MCP_PORT% > "%TUNNEL_LOG%" 2>&1"
      for /l %%i in (1,1,60) do (
        if not defined PUBLIC_URL (
          for /f "delims=" %%u in ('powershell -NoProfile -Command "if(Test-Path '%TUNNEL_LOG%'){(Select-String -Path '%TUNNEL_LOG%' -Pattern 'https://[a-zA-Z0-9._-]+\.trycloudflare\.com' -AllMatches ^| Select-Object -First 1).Matches.Value}"') do set "PUBLIC_URL=%%u"
          if not defined PUBLIC_URL timeout /t 1 /nobreak >nul
        )
      )
    )
  )
) else (
  echo [INFO]  ENABLE_TUNNEL=false - se omite el tunel.
)

echo.
echo =============================================================
echo    SERVIDOR MCP EN LINEA
echo =============================================================
echo   Local
echo     SSE             : http://127.0.0.1:%MCP_PORT%/sse
echo     Streamable HTTP : http://127.0.0.1:%MCP_PORT%/mcp
echo     Health          : http://127.0.0.1:%MCP_PORT%/health
if defined PUBLIC_URL (
  echo.
  echo   URL PUBLICA ^(Cloudflare Tunnel^)
  echo     Base        : !PUBLIC_URL!
  echo     MCP SSE     : !PUBLIC_URL!/sse
  echo     MCP Stream  : !PUBLIC_URL!/mcp
  echo.
  echo   Copilot Studio: usa !PUBLIC_URL!/sse como Server URL del conector MCP.
) else (
  echo.
  echo   [WARN] Sin URL publica. Revisa "%TUNNEL_LOG%"
)
echo.
echo   Logs: %SERVER_LOG%
echo         %TUNNEL_LOG%
echo   Cierra las ventanas "MCP-Server" y "cloudflared" para detener todo.
echo =============================================================
echo.
pause
endlocal
