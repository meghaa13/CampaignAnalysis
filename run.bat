@echo off
echo [INFO] Starting with venv Python...
call "%~dp0venv\Scripts\activate.bat"
.\venv\Scripts\python.exe app.py
