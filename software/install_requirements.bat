@echo off
REM Thin shim kept so existing Windows instructions keep working.
REM The real installer is install.py, which also runs on Linux.
REM   install.py --profile desktop|orin   install.py --check   install.py --help
py -3 "%~dp0install.py" %*
if errorlevel 1 exit /b 1
exit /b 0
