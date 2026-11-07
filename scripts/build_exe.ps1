# 打包 Windows 可执行程序（PyInstaller onedir）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "未找到 .venv。请先执行：python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}

& $Python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 PyInstaller ..."
    & $Python -m pip install pyinstaller
}

Write-Host "开始打包 ..."
& $Python -m PyInstaller --noconfirm --clean --onedir --name his-intel `
    --add-data "app\templates;app\templates" `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$Dist = Join-Path $Root "dist\his-intel"

# 配置与数据目录放在 exe 同级，便于人工维护（程序按 exe 所在目录解析 config/.env/data）
Copy-Item -Recurse -Force (Join-Path $Root "config") $Dist
if (-not (Test-Path (Join-Path $Dist ".env"))) {
    Copy-Item -Force (Join-Path $Root ".env.example") (Join-Path $Dist ".env")
}
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data\attachments") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data\exports") | Out-Null

Write-Host ""
Write-Host "构建完成：$Dist\his-intel.exe"
Write-Host "首次运行："
Write-Host "  cd `"$Dist`""
Write-Host "  .\his-intel.exe init-db"
Write-Host "  .\his-intel.exe collect"
Write-Host "  .\his-intel.exe serve"
