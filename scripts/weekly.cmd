@echo off
rem CCA Competitor Promo Report - corrida semanal (Programador de tareas, viernes 09:00).
rem   captura -> extraccion -> insights (Codex) -> PDF -> espera a mail.send_at (10:00 Panama) -> correo.
rem   Un fallo envia una alerta a mail.alert_to (o mail.to). Log: logs\weekly_AAAAMMDD_HHMM.log
cd /d "%~dp0.."
if not exist logs mkdir logs
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set TS=%%d
set COLUMNS=160
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m cca_report weekly %* >> "logs\weekly_%TS%.log" 2>&1
exit /b %errorlevel%
