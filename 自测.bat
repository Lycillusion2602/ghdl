@echo off
setlocal
cd /d "%~dp0"
title ghdl selftest
py -3 selftest.py
if errorlevel 9009 python selftest.py
pause
