[CmdletBinding()]
param(
    [string]$InstallDir = "C:\CameraIpTools",
    [switch]$ConfigureFirewall
)

$ErrorActionPreference = "Stop"
$packageDir = $PSScriptRoot
$requiredPackageFiles = @(
    "esc_vendor_bulk.py",
    "audit_camera_time.py",
    "probe_onvif_time.py",
    "set_onvif_time.py",
    "sync_camera_time.py",
    "camera_store.py",
    "camera_inventory_reader.py",
    "camera_gui.py",
    "camera_configurator.py",
    "camera_config.yaml",
    "profiles\cross.yaml",
    "python_packages\requests\__init__.py",
    "runtime\python.exe",
    "runtime\pythonw.exe",
    "vendor_bridge\VendorBridge.exe",
    "vendor_bridge\VendorBridge.cs",
    "vendor\Cameras.dll",
    "vendor\NvdcNetSDK.dll"
)

foreach ($relativePath in $requiredPackageFiles) {
    $path = Join-Path $packageDir $relativePath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Package file is missing: $relativePath"
    }
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "vendor_bridge") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "vendor") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "runtime") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "profiles") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "python_packages") -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $packageDir "esc_vendor_bulk.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "audit_camera_time.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "probe_onvif_time.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "set_onvif_time.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "sync_camera_time.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "camera_store.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "camera_inventory_reader.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "camera_gui.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "camera_configurator.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "camera_config.yaml") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $packageDir "requirements.txt") -Destination $InstallDir -Force
Copy-Item -Path (Join-Path $packageDir "profiles\*") -Destination (Join-Path $InstallDir "profiles") -Recurse -Force
Copy-Item -Path (Join-Path $packageDir "python_packages\*") -Destination (Join-Path $InstallDir "python_packages") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $packageDir "vendor_bridge\VendorBridge.exe") -Destination (Join-Path $InstallDir "vendor_bridge") -Force
Copy-Item -LiteralPath (Join-Path $packageDir "vendor_bridge\VendorBridge.cs") -Destination (Join-Path $InstallDir "vendor_bridge") -Force
Copy-Item -Path (Join-Path $packageDir "vendor\*") -Destination (Join-Path $InstallDir "vendor") -Recurse -Force
Copy-Item -Path (Join-Path $packageDir "runtime\*") -Destination (Join-Path $InstallDir "runtime") -Recurse -Force

$launcher = @'
@echo off
set "PYTHONPATH=%~dp0python_packages"
"%~dp0runtime\python.exe" "%~dp0esc_vendor_bulk.py" %*
'@
Set-Content -LiteralPath (Join-Path $InstallDir "camera-tool.cmd") -Value $launcher -Encoding ASCII

$guiLauncher = @'
@echo off
set "PYTHONPATH=%~dp0python_packages"
start "Camera IP Tools" "%~dp0runtime\pythonw.exe" "%~dp0camera_gui.py"
'@
Set-Content -LiteralPath (Join-Path $InstallDir "camera-gui.cmd") -Value $guiLauncher -Encoding ASCII

if ($ConfigureFirewall) {
    $firewallRules = @(
        @{ Name = "CameraIpTools ESC discovery"; Port = 31002 },
        @{ Name = "CameraIpTools DynaColor discovery"; Port = 6667 }
    )
    foreach ($firewallRule in $firewallRules) {
        if (-not (Get-NetFirewallRule -DisplayName $firewallRule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $firewallRule.Name -Direction Inbound -Action Allow -Protocol UDP -LocalPort $firewallRule.Port -Profile Domain,Private | Out-Null
        }
    }
    $programRules = @(
        @{ Name = "CameraIpTools console discovery program"; Program = (Join-Path $InstallDir "runtime\python.exe") },
        @{ Name = "CameraIpTools GUI discovery program"; Program = (Join-Path $InstallDir "runtime\pythonw.exe") },
        @{ Name = "CameraIpTools ESC SDK bridge program"; Program = (Join-Path $InstallDir "vendor_bridge\VendorBridge.exe") }
    )
    foreach ($firewallRule in $programRules) {
        if (-not (Get-NetFirewallRule -DisplayName $firewallRule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $firewallRule.Name -Direction Inbound -Action Allow -Program $firewallRule.Program -Protocol Any -Profile Domain,Private | Out-Null
        }
    }
}

$env:PYTHONPATH = Join-Path $InstallDir "python_packages"
& (Join-Path $InstallDir "runtime\python.exe") -m py_compile (Join-Path $InstallDir "esc_vendor_bulk.py") (Join-Path $InstallDir "audit_camera_time.py") (Join-Path $InstallDir "probe_onvif_time.py") (Join-Path $InstallDir "set_onvif_time.py") (Join-Path $InstallDir "sync_camera_time.py") (Join-Path $InstallDir "camera_store.py") (Join-Path $InstallDir "camera_inventory_reader.py") (Join-Path $InstallDir "camera_configurator.py") (Join-Path $InstallDir "camera_gui.py")
if ($LASTEXITCODE -ne 0) {
    throw "Python script validation failed."
}

$env:ESC_VENDOR_DIR = Join-Path $InstallDir "vendor"
& (Join-Path $InstallDir "vendor_bridge\VendorBridge.exe") self-test
if ($LASTEXITCODE -ne 0) {
    throw "The x86 bridge could not load the ESC SDK."
}

Write-Host ""
Write-Host "Installation completed: $InstallDir"
Write-Host "Discovery command:"
Write-Host "  $InstallDir\camera-tool.cmd scan --interface-ip SERVER_IP --timeout 6"
Write-Host "Graphical interface:"
Write-Host "  $InstallDir\camera-gui.cmd"
if (-not $ConfigureFirewall) {
    Write-Host "No firewall rule was created. Run setup with -ConfigureFirewall if required."
}
