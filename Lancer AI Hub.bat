@echo off
rem AI Hub : lance le serveur (sans fenetre) et ouvre l'appli dans le navigateur.
rem AIHUB_PORT : un autre port que 8770 ; AIHUB_SANS_NAVIGATEUR=1 : ne pas ouvrir le navigateur.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not defined AIHUB_PORT set "AIHUB_PORT=8770"
if not exist ".venv\Scripts\pythonw.exe" (
    echo  AI Hub n'est pas encore installe : lancez d'abord installer.bat.
    pause
    exit /b 1
)
if not exist data mkdir data
rem Deja lance : le serveur le dit et s'arrete ; le navigateur s'ouvre quand meme sur celui qui tourne.
start "" ".venv\Scripts\pythonw.exe" -m aihub --db "data\aihub.db" serve --port %AIHUB_PORT% --journal "data\serveur.log"
if "%AIHUB_SANS_NAVIGATEUR%"=="1" exit /b 0
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:%AIHUB_PORT%/"
exit /b 0
