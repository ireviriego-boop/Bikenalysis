@echo off
title Bikenalysis
cd /d "%~dp0analysis"

where python >nul 2>nul
if errorlevel 1 (
    echo No se encuentra Python instalado ^(o no esta en el PATH^).
    echo Instalalo desde https://python.org/downloads/ ^(marca "Add python.exe to PATH"^) y vuelve a intentarlo.
    pause
    exit /b 1
)

python -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo Instalando pywebview (una sola vez, para la ventana propia)...
    python -m pip install --quiet pywebview
)

python app.py

echo.
echo La aplicacion se ha cerrado. Pulsa una tecla para cerrar esta ventana.
pause >nul
