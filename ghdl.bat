@echo off
setlocal
cd /d "%~dp0"
title ghdl dev launcher
echo.
echo   ghdl - dev launcher
echo     1  run     start server and open browser (Ctrl+C or option 4 to stop)
echo     2  test     run selftest.py  (about 3 min, needs network)
echo     3  pack     build ghdl.exe with PyInstaller
echo     4  stop     ask a running server to exit
echo     5  version  print source version
echo     0  quit
set op=
set /p op=  pick: 
if "%op%"=="1" goto run
if "%op%"=="2" goto test
if "%op%"=="3" goto pack
if "%op%"=="4" goto stop
if "%op%"=="5" goto ver
if "%op%"=="0" goto end
echo   unknown choice
goto end
:run
py -3 server.py
if errorlevel 9009 python server.py
goto end
:test
py -3 selftest.py
if errorlevel 9009 python selftest.py
pause
goto end
:pack
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name ghdl --add-data "static;static" server.py
if errorlevel 9009 python -m PyInstaller --noconfirm --clean --onefile --windowed --name ghdl --add-data "static;static" server.py
if exist dist\ghdl.exe copy /y dist\ghdl.exe ghdl.exe
pause
goto end
:stop
py -3 server.py --stop
if errorlevel 9009 python server.py --stop
pause
goto end
:ver
py -3 server.py --version
if errorlevel 9009 python server.py --version
pause
goto end
:end
endlocal
