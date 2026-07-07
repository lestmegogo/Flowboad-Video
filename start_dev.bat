@echo off
title Flowboard Starter

echo ==========================================
echo       Flowboard Application Starter       
echo ==========================================
echo.

echo [1/2] Launching Flowboard Backend Agent (port 8101)...
if exist agent\.venv (
    start "Flowboard Backend" cmd /k "cd agent && .venv\Scripts\uvicorn flowboard.main:app --reload --port 8101"
) else (
    start "Flowboard Backend" cmd /k "cd agent && python -m uvicorn flowboard.main:app --reload --port 8101"
)

echo [2/2] Launching Flowboard Frontend Dev Server (port 5173)...
start "Flowboard Frontend" cmd /k "cd frontend && npm run dev"

echo.
echo ==========================================
echo Flowboard is starting up!
echo Backend and Frontend have been launched in separate windows.
echo ==========================================
echo.
pause
