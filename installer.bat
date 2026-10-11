@echo off
rem AI Hub : installation en un double-clic (Windows).
rem Prepare un environnement Python a cote de ce fichier (.venv), y installe les
rem dependances, cree un raccourci sur le Bureau, puis lance l'appli.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo  AI Hub : installation
echo  ---------------------

rem Python 3.12 ou plus : le lanceur « py » d'abord, sinon « python ».
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul || goto :nopython

if not exist ".venv\Scripts\python.exe" (
    echo  Preparation de l'environnement Python...
    %PY% -m venv .venv || goto :failed
)
echo  Installation des dependances (une seule fois, quelques Mo)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -r requirements.txt || goto :failed
if not exist data mkdir data

rem Un raccourci « AI Hub » sur le Bureau, qui lance l'appli.
set "AIHUB_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\AI Hub.lnk');" ^
  "$s.TargetPath = Join-Path $env:AIHUB_DIR 'Lancer AI Hub.bat'; $s.WorkingDirectory = $env:AIHUB_DIR;" ^
  "$s.Description = 'AI Hub'; $s.WindowStyle = 7; $s.Save()" >nul 2>nul

echo.
echo  Installe. Un raccourci « AI Hub » est sur le Bureau.
echo  Premier lancement : la page « Creer le serveur » s'ouvre dans le navigateur.
echo.
call "%~dp0Lancer AI Hub.bat"
exit /b 0

:nopython
echo.
echo  Python 3.12 ou plus est necessaire, et il n'est pas trouve.
echo  Installez-le depuis https://www.python.org/downloads/ (cochez « Add python.exe to PATH »),
echo  puis relancez installer.bat.
echo.
pause
exit /b 1

:failed
echo.
echo  L'installation a echoue (message ci-dessus). Verifiez la connexion Internet et relancez installer.bat.
echo.
pause
exit /b 1
