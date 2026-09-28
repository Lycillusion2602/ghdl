@echo off
setlocal
cd /d "%~dp0"
title ghdl - GitHub proxy downloader
py -3 server.py
if errorlevel 9009 python server.py
pause
