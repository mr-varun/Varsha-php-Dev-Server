@echo off
rem Run the test suite with this repo's venv.
rem   tools\test.cmd                                  all tests
rem   tools\test.cmd tests\test_manager.py -k crash   a subset
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m pytest -q %*
