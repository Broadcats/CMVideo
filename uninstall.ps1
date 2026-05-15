# CMVideo - Windows uninstaller
#
# Removes the Start Menu and Desktop shortcuts and (optionally) the
# project's .venv and portable bin/ folder. Does NOT uninstall system
# Python, ffmpeg, or espeak-ng - those might still be useful for other
# tools you have.

[CmdletBinding()]
param(
    [switch]$Yes  # answer 'yes' to all prompts (for scripted uninstall)
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName = "CMVideo"

function Confirm-Yes($msg) {
    if ($Yes) { return $true }
    $ans = Read-Host "$msg [y/N]"
    return ($ans -match '^(y|yes)$')
}

# 1. Shortcuts
$shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk")
)
foreach ($s in $shortcuts) {
    if (Test-Path $s) {
        Remove-Item $s -Force
        Write-Host "Removed $s"
    }
}

# 2. .venv (offer)
$venv = Join-Path $Here ".venv"
if (Test-Path $venv) {
    if (Confirm-Yes "Delete the local Python environment at .venv (frees ~1-2 GB)?") {
        Remove-Item $venv -Recurse -Force
        Write-Host "Removed .venv"
    }
}

# 3. Portable bin/ (offer)
$bin = Join-Path $Here "bin"
if (Test-Path $bin) {
    if (Confirm-Yes "Delete the portable ffmpeg / espeak-ng under .\bin\ ?") {
        Remove-Item $bin -Recurse -Force
        Write-Host "Removed .\bin\"
    }
}

Write-Host ""
Write-Host "Uninstalled. Project files are left where they are."
Write-Host "System Python, system ffmpeg, and system espeak-ng (if any) were not touched."
