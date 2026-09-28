@echo off
rem sdr - the command-line launcher for Automated SDR by Fred (Windows).
rem The installer puts a tiny sdr.cmd on your PATH that calls this file. It runs cli.py with the
rem project's own .venv Python (so the right requirements are always used) and passes every
rem argument through. It does not change folder, so relative paths keep their meaning.
setlocal
rem The bots print emoji. When output goes to a file or another program (an AI agent, "> log.txt"),
rem Windows Python would otherwise use the ANSI code page and stop at the first one. run_task.cmd
rem does the same for scheduled runs.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
for %%I in ("%~dp0..") do set "SDR_ROOT=%%~fI"
set "SDR_PY=%SDR_ROOT%\.venv\Scripts\python.exe"
if exist "%SDR_PY%" goto run
where py >nul 2>nul
if not errorlevel 1 goto run_launcher
set "SDR_PY=python"
where python >nul 2>nul
if not errorlevel 1 goto run
echo sdr: Python 3.11+ not found. Re-run install.ps1, or create "%SDR_ROOT%\.venv". 1>&2
exit /b 1

rem Each Python call shares one line with "exit /b" on purpose: `sdr update` can rewrite this
rem file while cmd.exe is still running it, and cmd re-reads the file by byte offset. "exit /b"
rem with no code keeps Python's exit code.
:run_launcher
py -3 "%SDR_ROOT%\cli.py" %* & exit /b

:run
"%SDR_PY%" "%SDR_ROOT%\cli.py" %* & exit /b
