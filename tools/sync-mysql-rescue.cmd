@echo off
rem Copy the MySQL rescue tool into bin\mysql-rescue\ (the "MySQL Rescue" page runs it from there).
rem   tools\sync-mysql-rescue.cmd "D:\path\to\MYSQL_RECOVERY"
rem Copies app\, runtime\ (its own Python) and the docs; never its jobs\, tests\, servers\, .git or
rem settings (varsha passes its own bin\mysql and bin\mariadb). Run it again after the tool changes,
rem then tools\deploy.cmd puts it into C:\Varsha too.
setlocal
if "%~1"=="" (
  echo usage: tools\sync-mysql-rescue.cmd "D:\path\to\MYSQL_RECOVERY"
  exit /b 2
)
set "SRC=%~1"
set "DST=%~dp0..\bin\mysql-rescue"
if not exist "%SRC%\app\mysql_rescue.py" (
  echo %SRC% doesn't look like the MySQL rescue tool ^(no app\mysql_rescue.py^)
  exit /b 1
)
robocopy "%SRC%\app" "%DST%\app" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
robocopy "%SRC%\runtime" "%DST%\runtime" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
for %%F in (README.md GUIDE.md) do if exist "%SRC%\%%F" copy /y "%SRC%\%%F" "%DST%\%%F" >nul
echo MySQL rescue copied to %DST%
exit /b 0
:failed
echo copying failed
exit /b 1
