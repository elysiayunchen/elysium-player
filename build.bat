@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ============================================================
rem  Elysium 打包脚本
rem  不硬编码任何个人路径，换机器、换用户都能直接跑。
rem  查找顺序：PYINSTALLER 环境变量，再到 PATH，再到 conda 常见位置
rem ============================================================

set PYI=
if defined PYINSTALLER set PYI=%PYINSTALLER%

if not defined PYI for %%I in (pyinstaller.exe) do if not "%%~$PATH:I"=="" set PYI=%%~$PATH:I

if not defined PYI for %%D in (miniconda3 anaconda3) do if exist "%USERPROFILE%\%%D\Scripts\pyinstaller.exe" set PYI=%USERPROFILE%\%%D\Scripts\pyinstaller.exe

if not defined PYI goto :nopyi

echo 使用打包器：%PYI%
echo === Elysium 打包 ===
"%PYI%" Elysium.spec --noconfirm
if errorlevel 1 goto :err

echo.
echo 完成：dist\Elysium.exe
pause
exit /b 0

:nopyi
echo.
echo [错误] 找不到 pyinstaller。
echo.
echo 三种解决办法任选一种：
echo    1. pip install pyinstaller
echo    2. 设置环境变量 PYINSTALLER 指向 pyinstaller.exe
echo    3. 用 conda 安装 pyinstaller
echo.
pause
exit /b 1

:err
echo.
echo 打包失败，请查看上方日志
pause
exit /b 1
