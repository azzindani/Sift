@echo off
rem Clipper installer (Windows).
rem
rem Clipper's deployment target is a Linux VPS; this exists so the server can be
rem developed and driven from a Windows desktop over stdio. ffmpeg must already be
rem on PATH.

setlocal enabledelayedexpansion

if "%CLIPPER_REPO%"=="" set CLIPPER_REPO=https://github.com/azzindani/Sift.git
if "%CLIPPER_HOME%"=="" set CLIPPER_HOME=%USERPROFILE%\.mcp_servers\Clipper

echo.
echo Clipper installer
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo error: git is not installed.
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo error: ffmpeg is not on PATH.
    echo        Install it from https://ffmpeg.org/download.html — a build with libx264 and libass.
    exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
    echo   uv not found — installing it
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

if exist "%CLIPPER_HOME%\.git" (
    echo   updating %CLIPPER_HOME%
    cd /d "%CLIPPER_HOME%"
    git fetch origin --quiet
    git reset --hard FETCH_HEAD --quiet
) else (
    echo   cloning into %CLIPPER_HOME%
    rem clone-guard: if it exists but is not a git checkout, replace it
    if exist "%CLIPPER_HOME%" rmdir /s /q "%CLIPPER_HOME%"
    git clone %CLIPPER_REPO% "%CLIPPER_HOME%" --quiet
    cd /d "%CLIPPER_HOME%"
)

echo   syncing dependencies
uv sync --quiet
if errorlevel 1 (
    echo error: uv sync failed.
    exit /b 1
)

if "%CLIPPER_VISION%"=="1" (
    echo   installing the vision extra ^(MediaPipe face-follow^)
    uv sync --extra vision --quiet
) else (
    echo   skipping MediaPipe ^(set CLIPPER_VISION=1 to enable face-follow reframing^)
)

echo.
echo Installed to %CLIPPER_HOME%
echo.
echo Add this to your MCP client config:
echo.
echo {
echo   "mcpServers": {
echo     "clipper": {
echo       "command": "uv",
echo       "args": ["--directory", "%CLIPPER_HOME%", "run", "python", "server.py", "--transport", "stdio"],
echo       "env": { "MCP_CONSTRAINED_MODE": "0" },
echo       "timeout": 600000
echo     }
echo   }
echo }
echo.

endlocal
