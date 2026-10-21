@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%TOKEN_ROUTER_PYTHON%"
if not defined PYTHON set "PYTHON=F:\uVenvs\ML_venv\Scripts\python.exe"
"%PYTHON%" -m token_router %*
exit /b %ERRORLEVEL%
