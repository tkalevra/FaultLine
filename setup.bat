@echo off
REM FaultLine quickstart launcher (Windows).
REM Delegates to the cross-platform Python wizard so there's one real implementation.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python quickstart.py %*
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    py quickstart.py %*
    goto :eof
)
echo Python 3.8+ is required. Install from https://www.python.org/downloads/ and re-run.
exit /b 1
