@echo off
rem Build do Sysvar Organizer para Windows (aplicativo empacotado + instalador).
rem Usa SOMENTE o Python do ambiente virtual do projeto (.venv); nada e
rem instalado automaticamente. Opcoes: --skip-installer, --iscc "caminho\ISCC.exe"
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [build] ERRO: ambiente virtual nao encontrado em "%ROOT%\.venv".
  echo [build] Crie-o com:  py -3.12 -m venv .venv
  echo [build] e instale:   .venv\Scripts\python.exe -m pip install -e . -r packaging\scripts\requirements-build.txt
  exit /b 3
)
"%PY%" "%ROOT%\packaging\scripts\build_windows.py" %*
exit /b %ERRORLEVEL%
