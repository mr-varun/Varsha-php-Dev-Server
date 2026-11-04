@echo off
rem Build Varsha.exe from this repo and put the final app into C:\Varsha (see tools\deploy.py).
"%~dp0..\.venv\Scripts\python.exe" "%~dp0deploy.py" %*
