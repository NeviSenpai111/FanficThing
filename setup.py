#!/usr/bin/env python3
"""Cross-platform setup script for Fanficthing."""

import os
import platform
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WIN = platform.system() == "Windows"
VENV_DIR = os.path.join(ROOT, ".venv")

if IS_WIN:
    PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe")
    PIP = os.path.join(VENV_DIR, "Scripts", "pip.exe")
    PLAYWRIGHT = os.path.join(VENV_DIR, "Scripts", "playwright.exe")
else:
    PYTHON = os.path.join(VENV_DIR, "bin", "python")
    PIP = os.path.join(VENV_DIR, "bin", "pip")
    PLAYWRIGHT = os.path.join(VENV_DIR, "bin", "playwright")


def step(msg):
    print(f"\n{'='*50}")
    print(f"  {msg}")
    print(f"{'='*50}")


def run(cmd, **kwargs):
    print(f"  > {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    subprocess.check_call(cmd, **kwargs)


def main():
    print("""
    ___                __  _      __  __    _
   / __\\__ _ _ __  / _(_) ___| |_| |__ (_)_ __   __ _
  / _\\/ _` | '_ \\| |_| |/ __| __| '_ \\| | '_ \\ / _` |
 / / | (_| | | | |  _| | (__| |_| | | | | | | | (_| |
 \\/   \\__,_|_| |_|_| |_|\\___|\\__|_| |_|_|_| |_|\\__, |
                                                 |___/
    """)

    # 1. Create virtual environment
    step("Creating virtual environment")
    if os.path.exists(VENV_DIR):
        print("  Virtual environment already exists, skipping.")
    else:
        venv.create(VENV_DIR, with_pip=True)
        print("  Done.")

    # 2. Install dependencies
    step("Installing Python dependencies")
    run([PYTHON, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([PIP, "install", "-r", os.path.join(ROOT, "requirements.txt")])

    # 3. Install Playwright browser
    step("Installing Playwright Chromium browser")
    run([PLAYWRIGHT, "install", "chromium"])

    # 4. Create data directory
    step("Creating data directory")
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    print("  Done.")

    # 5. Create start script for Windows if needed
    if IS_WIN:
        step("Creating start.bat")
        bat_path = os.path.join(ROOT, "start.bat")
        with open(bat_path, "w") as f:
            f.write('@echo off\n')
            f.write('cd /d "%~dp0"\n')
            f.write('echo Starting Fanficthing on http://localhost:8000\n')
            f.write('.venv\\Scripts\\uvicorn.exe app:app --host 0.0.0.0 --port 8000\n')
        print(f"  Created {bat_path}")

    # Done
    print(f"""
{'='*50}
  Setup complete!
{'='*50}

  To start Fanficthing:
""")
    if IS_WIN:
        print("    Double-click start.bat")
        print("    Or run: .venv\\Scripts\\uvicorn.exe app:app --host 0.0.0.0 --port 8000")
    else:
        print("    ./start.sh")
        print("    Or run: .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000")

    print("\n  Then open http://localhost:8000 in your browser.\n")


if __name__ == "__main__":
    main()
