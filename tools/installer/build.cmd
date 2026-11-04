@echo off
rem Builds Setup.exe with the C# compiler that ships with Windows (.NET Framework 4).
rem   build.cmd <output folder>     (tools\deploy.py passes Varsha-Setup)
setlocal
set "ROOT=%~dp0..\.."
set "OUT=%~1"
if "%OUT%"=="" set "OUT=%ROOT%\Varsha-Setup"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%OUT%" mkdir "%OUT%"
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu ^
  /win32icon:"%ROOT%\src\varsha\web\static\varsha.ico" ^
  /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ^
  /reference:Microsoft.CSharp.dll /reference:System.Core.dll ^
  /out:"%OUT%\Setup.exe" "%~dp0Setup.cs"
if errorlevel 1 (echo Build failed & exit /b 1)
echo Built "%OUT%\Setup.exe"
