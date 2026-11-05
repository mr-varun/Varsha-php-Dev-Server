@echo off
rem Copy the MDB rescue tool into bin\mdb-rescue\ (the Data page's "MDB Rescue" runs it from there).
rem   tools\sync-mdb-rescue.cmd "D:\path\to\MDB_RECOVERY"
rem Copies app\, runtime\ (its own 64/32-bit Python) and the docs; never its jobs\, tests\, .git or
rem settings. Run it again after the tool changes, then tools\deploy.cmd puts it into C:\Varsha too.
setlocal
if "%~1"=="" (
  echo usage: tools\sync-mdb-rescue.cmd "D:\path\to\MDB_RECOVERY"
  exit /b 2
)
set "SRC=%~1"
set "DST=%~dp0..\bin\mdb-rescue"
if not exist "%SRC%\app\mdb_rescue.py" (
  echo %SRC% doesn't look like the MDB rescue tool ^(no app\mdb_rescue.py^)
  exit /b 1
)
robocopy "%SRC%\app" "%DST%\app" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
robocopy "%SRC%\runtime" "%DST%\runtime" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
for %%F in (README.md GUIDE.md) do if exist "%SRC%\%%F" copy /y "%SRC%\%%F" "%DST%\%%F" >nul
echo MDB rescue copied to %DST%
exit /b 0
:failed
echo copying failed
exit /b 1
