@echo off
setlocal
cd /d "%~dp0"
title ghdl pack (onefile windowed)
echo building ghdl.exe with PyInstaller ...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name ghdl --add-data "static;static" server.py
if errorlevel 9009 python -m PyInstaller --noconfirm --clean --onefile --windowed --name ghdl --add-data "static;static" server.py
if exist dist\ghdl.exe copy /y dist\ghdl.exe ghdl.exe
if exist ghdl.exe echo done: ghdl.exe is in this folder
pause
