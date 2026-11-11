# 打包 Windows 可执行程序（PyInstaller onedir）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 注意：本文件必须保存为「UTF-8 带 BOM」，否则 Windows PowerShell 5.1 会按 GBK 解析导致中文字符串报错。

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

# 图标：多尺寸 .ico（exe / 任务栏 / 资源管理器统一）。缺失则回退默认图标并给出提示。
$IconArgs = @()
$Icon = Join-Path $Root "assets\app.ico"
if (Test-Path $Icon) {
    $IconArgs = @("--icon", $Icon)
} else {
    Write-Host "[提示] 未找到 assets\app.ico，exe 将使用默认图标。请放置多尺寸 assets\app.ico 后重新运行。" -ForegroundColor Yellow
}

Write-Host "开始打包 ..."
& $Python -m PyInstaller --noconfirm --clean --onedir --name his-intel `
    --add-data "app\templates;app\templates" `
    --add-data "app\static;app\static" `
    @IconArgs `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$Dist = Join-Path $Root "dist\his-intel"

# 配置与数据目录放在 exe 同级，便于人工维护（程序按 exe 所在目录解析 config/.env/data）
Copy-Item -Recurse -Force (Join-Path $Root "config") $Dist
if (Test-Path $Icon) { Copy-Item -Force $Icon (Join-Path $Dist "app.ico") }
if (-not (Test-Path (Join-Path $Dist ".env"))) {
    Copy-Item -Force (Join-Path $Root ".env.example") (Join-Path $Dist ".env")
}
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data\attachments") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data\exports") | Out-Null

# 版本号：从 app/version.py 读取，产物命名 his-intel-vX.Y.Z-win64.zip（解压覆盖即可升级，data/ 不在包内）
$VerSource = Get-Content (Join-Path $Root "app\version.py") -Raw -Encoding UTF8
$VerMatch = [regex]::Match($VerSource, '__version__\s*=\s*"([\d.]+)"')
if (-not $VerMatch.Success) { throw "无法从 app\version.py 解析版本号" }
$Ver = $VerMatch.Groups[1].Value
$Zip = Join-Path $Root "dist\his-intel-v$Ver-win64.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
# 打包整个 his-intel 目录（解压后即为 his-intel 文件夹，覆盖旧目录完成升级）
Compress-Archive -Path $Dist -DestinationPath $Zip -CompressionLevel Optimal

Write-Host ""
Write-Host "构建完成：$Dist\his-intel.exe"
Write-Host "升级包：  $Zip"
Write-Host "首次运行："
Write-Host "  cd `"$Dist`""
Write-Host "  .\his-intel.exe init-db"
Write-Host "  .\his-intel.exe collect"
Write-Host "  .\his-intel.exe serve"
