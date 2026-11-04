@echo off
rem Builds C:\Varsha\Varsha.exe with the C# compiler that ships with Windows (.NET Framework 4).
setlocal
set "ROOT=%~dp0..\.."
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu ^
  /win32icon:"%ROOT%\src\varsha\web\static\varsha.ico" ^
  /reference:System.Windows.Forms.dll ^
  /out:"%ROOT%\Varsha.exe" "%~dp0Varsha.cs"
if errorlevel 1 (echo Build failed & exit /b 1)
echo Built "%ROOT%\Varsha.exe"
