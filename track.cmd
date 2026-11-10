@echo off
rem Scheduled price tracking: records prices only (--dry-run = no Telegram/WhatsApp posts).
rem Remove --dry-run when you want deals posted automatically.
cd /d "%~dp0"
if not exist logs mkdir logs
"C:\Python314\python.exe" -m skd.run --dry-run --maccaron-delay 1 >> "logs\track.log" 2>&1
