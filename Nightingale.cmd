@echo off
rem Start Nightingale in the background (tray icon, no window). Double-click this file.
cd /d "%~dp0"
start "" pythonw -m app.tray
