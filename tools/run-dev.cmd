@echo off
rem Run the code in THIS repo from source, against this repo's own bin, config, data and www.
rem (C:\Varsha is the deployed build; it has its own data. Only one varsha runs per folder.)
setlocal
set "VARSHA_ROOT="
set "PYTHONPATH=%~dp0..\src"
"%~dp0..\.venv\Scripts\python.exe" -m varsha %*
