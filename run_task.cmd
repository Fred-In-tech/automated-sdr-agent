@echo off
rem Runs a task for Windows Task Scheduler. Installed by "sdr schedule on" (see core\scheduler.py).
rem The Windows twin of run_cron_pipeline.sh.
rem   run_task.cmd            full SDR pipeline: inbox, new leads, emails, daily digest
rem   run_task.cmd inbox      quick reply check only (sends no cold email)
rem   run_task.cmd update     check for a new version (cli.py update --scheduled)
setlocal EnableExtensions DisableDelayedExpansion

set "TASK=%~1"
if "%TASK%"=="" set "TASK=pipeline"
if /I "%TASK%"=="pipeline" (set "TASK=pipeline" & goto task_ok)
if /I "%TASK%"=="inbox" (set "TASK=inbox" & goto task_ok)
if /I "%TASK%"=="update" (set "TASK=update" & goto task_ok)
echo Unknown task. Use one of: pipeline, inbox, update 1>&2
exit /b 2

:task_ok
set "ROOT=%~dp0"
cd /d "%ROOT%"
if not exist "%ROOT%data" mkdir "%ROOT%data"
set "LOG=%ROOT%data\cron.log"

rem Keep the log small: rotate at 5 MB (one previous copy kept as cron.log.1)
if exist "%LOG%" for %%F in ("%LOG%") do if %%~zF GTR 5000000 move /Y "%LOG%" "%LOG%.1" >nul

rem The bots print emoji; without UTF-8, Python crashes writing them to the log file.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Use the first Python (3.11+) that has this project's requirements installed.
rem Override with a PYTHON environment variable (full path to python.exe) if needed.
set "PY="
set "PYARGS="
if defined PYTHON set "PY=%PYTHON%"
if defined PY goto run
if exist "%ROOT%.venv\Scripts\python.exe" call :try_python "%ROOT%.venv\Scripts\python.exe"
if defined PY goto run
where py >nul 2>&1 && call :try_python py -3
if defined PY goto run
where python >nul 2>&1 && call :try_python python
if defined PY goto run
echo === %DATE% %TIME% - ERROR: no Python 3.11+ with requirements found. Run: pip install -r requirements.txt === >> "%LOG%"
exit /b 1

rem The Python call and "exit /b" share one line on purpose. cmd.exe re-reads a batch file by
rem byte offset after every command, and "update" may replace this very file while Python
rem runs; a separate exit line could then be read from the new file at the old offset and run
rem a fragment of some other line. "exit /b" with no code keeps Python's exit code.
:run
echo === %DATE% %TIME% - running %TASK% with "%PY%" %PYARGS% === >> "%LOG%"
if "%TASK%"=="update" goto run_update
"%PY%" %PYARGS% runner.py --task %TASK% >> "%LOG%" 2>&1 & exit /b

:run_update
"%PY%" %PYARGS% cli.py update --scheduled >> "%LOG%" 2>&1 & exit /b

:try_python
rem %1 = python executable, %2 = optional launcher argument (py -3).
rem tomllib only exists on Python 3.11+, so this also checks the version.
"%~1" %2 -c "import tomllib, requests, bs4, dns.resolver" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
set "PYARGS=%~2"
exit /b 0
