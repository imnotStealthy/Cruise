@echo off
REM Builds Cruise.exe: native window (PyWebView), no browser.
cd /d "%~dp0"
REM No UPX: it corrupts lazily-imported modules in the onefile archive
REM (zlib "incorrect header check" at runtime). Reliability over ~2 MB.
py -m pip install --quiet --upgrade pyinstaller
py -m PyInstaller --noconfirm --noconsole --onefile --name Cruise ^
  --icon web\icon.ico ^
  --exclude-module numpy --exclude-module cv2 --exclude-module tkinter ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  --exclude-module pyautogui --exclude-module pyscreeze --exclude-module PIL ^
  --exclude-module fastapi --exclude-module uvicorn --exclude-module starlette ^
  --exclude-module pydantic --exclude-module pydantic_core ^
  --add-data "web;web" ^
  --add-data "config.json;." ^
  --add-data "cars.json;." ^
  --collect-all webview ^
  --collect-all vgamepad ^
  --collect-submodules pydirectinput ^
  desktop.py
copy /Y config.json dist\config.json >nul
echo.
echo Done. Executable: dist\Cruise.exe  (native window, single file)
pause
