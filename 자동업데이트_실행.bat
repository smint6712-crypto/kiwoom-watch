@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo checking update...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 -Uri 'https://raw.githubusercontent.com/smint6712-crypto/kiwoom-watch/main/kiwoom_1025.py' -OutFile 'kiwoom_1025.py.new'; if (Select-String -Path 'kiwoom_1025.py.new' -Pattern 'class KTable' -Quiet) { Move-Item -Force 'kiwoom_1025.py.new' 'kiwoom_1025.py'; Write-Host 'updated' } else { Remove-Item -Force 'kiwoom_1025.py.new'; Write-Host 'skip: bad file' } } catch { Write-Host 'no update (offline or error), using current file' }"
start "" pythonw kiwoom_1025.py
