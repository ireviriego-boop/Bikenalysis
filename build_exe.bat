@echo off
title Bikenalysis - generar .exe
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo No se encuentra Python instalado ^(o no esta en el PATH^).
    echo Instalalo desde https://python.org/downloads/ ^(marca "Add python.exe to PATH"^) y vuelve a intentarlo.
    pause
    exit /b 1
)

echo Instalando/actualizando pywebview, pyinstaller y sus hooks...
python -m pip install --quiet --upgrade pywebview pyinstaller pyinstaller-hooks-contrib
if errorlevel 1 (
    echo Fallo instalando las dependencias de compilacion.
    pause
    exit /b 1
)

echo.
echo Compilando Bikenalysis.exe (puede tardar uno o dos minutos)...
python -m PyInstaller --noconfirm --name Bikenalysis --onefile --windowed ^
    --paths analysis --paths . ^
    --add-data "analysis/static;static" ^
    analysis/app.py

if errorlevel 1 (
    echo.
    echo La compilacion ha fallado -- revisa los mensajes de arriba.
    pause
    exit /b 1
)

echo.
echo Listo: dist\Bikenalysis.exe
echo Puedes mover ese fichero (o copiarlo, p.ej. al escritorio) donde quieras --
echo es autocontenido, no necesita Python instalado. La primera vez que lo
echo abras desde su nueva ubicacion creara junto a el las carpetas/ficheros de
echo datos (data\, fit_files\, tokens.json...) y te pedira autorizar la app en
echo Hammerhead si no los llevas contigo.
pause
