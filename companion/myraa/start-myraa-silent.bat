@echo off
setlocal

set "MYRAA_DIR=%~dp0"
set "MYRAA_ELECTRON=%MYRAA_DIR%node_modules\electron\dist\electron.exe"
set "MYRAA_SERVER=%MYRAA_DIR%dist\server.cjs"

if not exist "%MYRAA_ELECTRON%" (
  echo MYRAA dependencies are missing. Run npm install from "%MYRAA_DIR%" first.
  exit /b 1
)

if not exist "%MYRAA_SERVER%" (
  echo MYRAA has not been built. Run npm run build from "%MYRAA_DIR%" first.
  exit /b 1
)

start "" "%MYRAA_ELECTRON%" "%MYRAA_DIR%"
exit /b 0
