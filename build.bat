@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
python -m pip install pyinstaller

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

pyinstaller --clean --noconfirm GBFRRelinkEditor.spec

if exist "dist\GBFRRelinkEditor.exe" (
    echo.
    echo Built one-file EXE:
    echo %CD%\dist\GBFRRelinkEditor.exe
) else (
    echo.
    echo Build finished, but dist\GBFRRelinkEditor.exe was not found.
)

echo.
pause
