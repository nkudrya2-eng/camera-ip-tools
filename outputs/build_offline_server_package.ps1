[CmdletBinding()]
param(
    [string]$PythonHome = "C:\Users\n.kudrya\AppData\Local\Programs\Python\Python310",
    [string]$EscDir = "C:\Program Files (x86)\ESC\lib\Starter",
    [string]$OutputZip = ""
)

$ErrorActionPreference = "Stop"
$sourceDir = $PSScriptRoot
$configuratorDir = Join-Path (Split-Path $sourceDir -Parent) "AutoStartSettingWork_v0.02"
if ([string]::IsNullOrWhiteSpace($OutputZip)) {
    $OutputZip = Join-Path $sourceDir "CameraIpTools_WindowsServer_Offline.zip"
}
$templateDir = Join-Path $sourceDir "windows_server_package"
$stageRoot = Join-Path $sourceDir ".server_package_build"
$stageDir = Join-Path $stageRoot "CameraIpTools_WindowsServer_Offline"

$resolvedStageRoot = [System.IO.Path]::GetFullPath($stageRoot)
$resolvedSourceDir = [System.IO.Path]::GetFullPath($sourceDir)
if (-not $resolvedStageRoot.StartsWith($resolvedSourceDir, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Build directory is outside outputs: $resolvedStageRoot"
}

if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "python.exe"))) {
    throw "Python source runtime was not found: $PythonHome"
}
if (-not (Test-Path -LiteralPath (Join-Path $EscDir "NvdcNetSDK.dll"))) {
    throw "ESC SDK was not found: $EscDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $configuratorDir "camera_configurator.py"))) {
    throw "Camera configurator was not found: $configuratorDir"
}

if (Test-Path -LiteralPath $stageRoot) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "vendor_bridge") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "vendor") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "runtime") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "profiles") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageDir "python_packages") -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $sourceDir "esc_vendor_bulk.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "audit_camera_time.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "probe_onvif_time.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "set_onvif_time.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "sync_camera_time.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "camera_store.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "camera_inventory_reader.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "camera_gui.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $configuratorDir "camera_configurator.py") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $configuratorDir "camera_config.yaml") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $configuratorDir "requirements.txt") -Destination $stageDir
Copy-Item -Path (Join-Path $configuratorDir "profiles\*") -Destination (Join-Path $stageDir "profiles") -Recurse
Copy-Item -LiteralPath (Join-Path $sourceDir "vendor_bridge\VendorBridge.exe") -Destination (Join-Path $stageDir "vendor_bridge")
Copy-Item -LiteralPath (Join-Path $sourceDir "vendor_bridge\VendorBridge.cs") -Destination (Join-Path $stageDir "vendor_bridge")
Copy-Item -LiteralPath (Join-Path $templateDir "install.ps1") -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $templateDir "SERVER_README.txt") -Destination $stageDir
Copy-Item -Path (Join-Path $EscDir "*.dll") -Destination (Join-Path $stageDir "vendor")

$runtimeDir = Join-Path $stageDir "runtime"
Copy-Item -LiteralPath (Join-Path $PythonHome "python.exe") -Destination $runtimeDir
Copy-Item -LiteralPath (Join-Path $PythonHome "pythonw.exe") -Destination $runtimeDir
Copy-Item -LiteralPath (Join-Path $PythonHome "python3.dll") -Destination $runtimeDir
Copy-Item -LiteralPath (Join-Path $PythonHome "python310.dll") -Destination $runtimeDir
Copy-Item -LiteralPath (Join-Path $PythonHome "LICENSE.txt") -Destination $runtimeDir
Copy-Item -Path (Join-Path $PythonHome "vcruntime*.dll") -Destination $runtimeDir
Copy-Item -Path (Join-Path $PythonHome "*.dll") -Destination $runtimeDir
Copy-Item -LiteralPath (Join-Path $PythonHome "DLLs") -Destination $runtimeDir -Recurse
Copy-Item -LiteralPath (Join-Path $PythonHome "tcl") -Destination $runtimeDir -Recurse

$runtimeLib = Join-Path $runtimeDir "Lib"
New-Item -ItemType Directory -Path $runtimeLib -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PythonHome "Lib") -Force |
    Where-Object { $_.Name -notin @("site-packages", "test", "tkinter", "idlelib") } |
    Copy-Item -Destination $runtimeLib -Recurse
Copy-Item -LiteralPath (Join-Path $PythonHome "Lib\tkinter") -Destination $runtimeLib -Recurse

& (Join-Path $PythonHome "python.exe") -m pip install --disable-pip-version-check --target (Join-Path $stageDir "python_packages") requests==2.32.5 PyYAML==6.0.3
if ($LASTEXITCODE -ne 0) {
    throw "Could not package GUI Python dependencies."
}

$env:PYTHONPATH = Join-Path $stageDir "python_packages"
& (Join-Path $runtimeDir "python.exe") -m py_compile (Join-Path $stageDir "esc_vendor_bulk.py") (Join-Path $stageDir "audit_camera_time.py") (Join-Path $stageDir "probe_onvif_time.py") (Join-Path $stageDir "set_onvif_time.py") (Join-Path $stageDir "sync_camera_time.py") (Join-Path $stageDir "camera_store.py") (Join-Path $stageDir "camera_inventory_reader.py") (Join-Path $stageDir "camera_configurator.py") (Join-Path $stageDir "camera_gui.py")
if ($LASTEXITCODE -ne 0) {
    throw "Portable Python could not run the tool."
}
$env:ESC_VENDOR_DIR = Join-Path $stageDir "vendor"
& (Join-Path $stageDir "vendor_bridge\VendorBridge.exe") self-test
if ($LASTEXITCODE -ne 0) {
    throw "The bridge could not load the packaged SDK."
}

if (Test-Path -LiteralPath $OutputZip) {
    Remove-Item -LiteralPath $OutputZip -Force
}
Compress-Archive -LiteralPath $stageDir -DestinationPath $OutputZip -CompressionLevel Optimal
Remove-Item -LiteralPath $stageRoot -Recurse -Force

$zip = Get-Item -LiteralPath $OutputZip
Write-Host "Offline package ready: $($zip.FullName)"
Write-Host "Size: $([math]::Round($zip.Length / 1MB, 1)) MB"
