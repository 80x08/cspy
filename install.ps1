# Copies dist\ into CLIP STUDIO PAINT. Run from an elevated PowerShell (Program Files), with CSP closed.
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1 [-CspDir <path>] [-Uninstall]
# Scripts you added to <CSP>\cspy\scripts are kept (files are only added/overwritten, never deleted).
param(
    [string]$CspDir = "C:\Program Files\CELSYS\CLIP STUDIO 1.5\CLIP STUDIO PAINT",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path "$CspDir\CLIPStudioPaint.exe")) { throw "CLIPStudioPaint.exe not found in $CspDir" }
if (Get-Process CLIPStudioPaint -ErrorAction SilentlyContinue) { throw "close CLIP STUDIO PAINT first" }

if ($Uninstall) {
    # Removing the plugin DLL is enough to stop loading; the cspy folder (and your scripts) stays.
    Remove-Item "$CspDir\PlugIn\PAINT\cspy.dll" -ErrorAction SilentlyContinue
    "uninstalled (left $CspDir\cspy in place)"
    return
}
if (-not (Test-Path "$PSScriptRoot\dist\PlugIn\PAINT\cspy.dll")) {
    throw "no dist\ next to install.ps1. Keep install.ps1 beside the dist folder (or run build.ps1), " +
          "or skip this script: copying dist\'s PlugIn and cspy folders into $CspDir is the whole install."
}
robocopy "$PSScriptRoot\dist" $CspDir /E /XD __pycache__ /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw "copy failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0
"installed into $CspDir"
