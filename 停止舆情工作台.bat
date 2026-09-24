@echo off
setlocal
cd /d "%~dp0"
if not exist "launch.py" (
  echo Application source was not found.
  pause
  exit /b 1
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 -B launch.py --stop
  if errorlevel 1 goto fail
  exit /b 0
)
py -3.12 -c "import streamlit, pandas, openpyxl, requests, xlrd" >nul 2>nul
if not errorlevel 1 (
  py -3.12 -X utf8 -B launch.py --stop
  if errorlevel 1 goto fail
  exit /b 0
)
python -c "import sys; assert sys.version_info[:2] == (3, 12); import streamlit, pandas, openpyxl, requests, xlrd" >nul 2>nul
if not errorlevel 1 (
  python -X utf8 -B launch.py --stop
  if errorlevel 1 goto fail
  exit /b 0
)
echo Python dependencies were not found. Run the dependency installer first.
pause
exit /b 1
:fail
echo Stop request failed. Check the output above.
pause
exit /b 1
