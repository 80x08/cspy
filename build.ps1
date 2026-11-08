# Builds dist\, laid out like the CSP install folder:
#   dist\PlugIn\PAINT\cspy.dll   loader (CSP auto-loads it as a plugin)
#   dist\cspy\                   runtime .py files + scripts\ + embedded Python 3.14
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$vcvars = "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
$pyZip = "$root\build\python-3.14.7-embed-amd64.zip"
$pyUrl = "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip"
$pySha = "D297E5FF019966817AD8502465176139F2D3D840FA4ED84B13BED399A6AB1F15"

New-Item -ItemType Directory -Force "$root\build", "$root\dist\cspy", "$root\dist\PlugIn\PAINT" | Out-Null

if (-not (Test-Path $pyZip)) { Invoke-WebRequest $pyUrl -OutFile $pyZip }
if ((Get-FileHash $pyZip -Algorithm SHA256).Hash -ne $pySha) { throw "hash mismatch, delete and re-download: $pyZip" }

Push-Location "$root\build"
try {
    cmd /c "`"$vcvars`" >nul 2>nul && cl /nologo /LD /O2 /W4 /D_CRT_SECURE_NO_WARNINGS ..\src\loader\loader.c /Fe:cspy.dll /link user32.lib"
    if ($LASTEXITCODE) { throw "compile failed" }
} finally { Pop-Location }
Copy-Item "$root\build\cspy.dll" "$root\dist\PlugIn\PAINT\" -Force

robocopy "$root\src\cspy" "$root\dist\cspy" /E /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying runtime failed ($LASTEXITCODE)" }
if (-not (Test-Path "$root\dist\cspy\python\python314.dll")) { Expand-Archive $pyZip "$root\dist\cspy\python" }
$global:LASTEXITCODE = 0
"built $root\dist"
