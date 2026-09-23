import sys
import os
import re

WORK_DIR = r'Z:\workspace\5050-SC_Monitor-dev-2yuanshop'
sys.path.insert(0, WORK_DIR)
os.chdir(WORK_DIR)

log_file = open(os.path.join(WORK_DIR, 'build.log'), 'w', encoding='utf-8')
sys.stdout = log_file
sys.stderr = log_file

print("开始打包...")
print(f"Python: {sys.version}")

APP_EXE_NAME = "BV号监听器"
PROJECT_FILE = os.path.join(WORK_DIR, 'pyproject.toml')
VERSION_ENV_VAR = "SC_MONITOR_VERSION"
VERSION_HOOK_FILE = os.path.join(WORK_DIR, '_pyinstaller_version_hook.py')

with open(PROJECT_FILE, 'r', encoding='utf-8') as f:
    content = f.read()
match = re.search(r'version\s*=\s*"([^"]+)"', content)
app_version = match.group(1) if match else "0.0.1"
print(f"版本: {app_version}")

with open(VERSION_HOOK_FILE, 'w', encoding='utf-8') as f:
    f.write(f'import os\nos.environ[{VERSION_ENV_VAR!r}] = {app_version!r}\n')

app_name = f"{APP_EXE_NAME} [{app_version}]"

from PyInstaller.__main__ import run

run([
    "--clean",
    "--onefile",
    "--noupx",
    "--windowed",
    "--name",
    app_name,
    "--icon",
    os.path.join(WORK_DIR, 'resources', 'favicon.ico'),
    "--runtime-hook",
    VERSION_HOOK_FILE,
    "--add-data",
    f"{os.path.join(WORK_DIR, 'resources', 'favicon.ico')};resources",
    "--collect-all",
    "qrcode",
    "--collect-submodules",
    "PIL",
    os.path.join(WORK_DIR, 'main.py'),
])

print("打包完成！")
log_file.close()
