<#
.SYNOPSIS
    Prepare a Batocera SD card for the PiBoy DMG (Raspberry Pi 4B, 3B/3B+).

.DESCRIPTION
    Run it on your PC right after flashing Batocera, before the first boot.
    It only edits config.txt on the BATOCERA partition:
      - the untouched file is saved once as config.txt.orig;
      - "dtoverlay=vc4-kms-v3d" is commented out. Stock Batocera loads this
        full KMS display driver; it takes the screen over right after the
        splash and cannot drive the PiBoy's DPI panel (splash, then black);
      - any previous PiBoy block is removed (ours, or the one Batocera writes
        itself for its "PIBOY" power switch option), then the block for your
        Pi is appended at the end.
    The Pi model comes from the image (boot\batocera.board). Safe to run again.

.PARAMETER Drive
    Drive letter of the BATOCERA partition, e.g. E:

.PARAMETER Pi
    4 or 3, only if the model cannot be read from the image.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File boot\prepare-sd.ps1 -Drive E:
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Drive,
    [ValidateSet('', '3', '4')][string]$Pi = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$Drive = $Drive.TrimEnd('\')
if ($Drive -match '^[A-Za-z]$') { $Drive += ':' }
if ($Drive -notmatch '^[A-Za-z]:$') { throw "Expected a drive letter such as E: (got '$Drive')" }
$cfg = Join-Path $Drive 'config.txt'
if (-not (Test-Path $cfg) -or -not (Test-Path (Join-Path $Drive 'batocera-boot.conf'))) {
    throw "$Drive does not look like a Batocera boot partition (config.txt and batocera-boot.conf expected). Nothing written."
}

if (-not $Pi) {
    $boardFile = Join-Path $Drive 'boot\batocera.board'
    $board = ''
    if (Test-Path $boardFile) { $board = ([System.IO.File]::ReadAllText($boardFile)).Trim() }
    switch ($board) {
        'bcm2711' { $Pi = '4' }
        'bcm2837' { $Pi = '3' }
        'bcm2836' { $Pi = '3' }
        'bcm2712' { throw 'This is the Raspberry Pi 5 image (bcm2712). The PiBoy DMG needs a Pi 4 or Pi 3 and the matching image: bcm2711 or bcm2837.' }
        default   { throw "Unknown board '$board' in $boardFile. Add -Pi 4 or -Pi 3 if you are sure of your image." }
    }
}
$blockFile = Join-Path $here "config-piboy-pi$Pi.txt"
if (-not (Test-Path $blockFile)) { throw "$blockFile is missing" }
Write-Host "Target: $cfg (Raspberry Pi $Pi)"

function Read-LF([string]$Path) {
    # Normalise line endings BEFORE any regex: in .NET a multiline `$` stops
    # before \n but keeps the \r in the capture, which splits edited lines.
    $t = [System.IO.File]::ReadAllText($Path)
    return ($t -replace "`r`n", "`n") -replace "`r", "`n"
}

$orig = "$cfg.orig"
if (-not (Test-Path $orig)) {
    Copy-Item $cfg $orig
    Write-Host '  original saved as config.txt.orig'
}

$content = Read-LF $cfg
$content = [regex]::Replace($content, '(?ms)^# ====== PiBoy DMG - Raspberry Pi.*?^# ====== end of PiBoy DMG block[^\n]*(\n|$)', '')
$content = [regex]::Replace($content, '(?ms)^# ====== PiBoy Case setup section.*?^# ====== PiBoy Case toggle section[^\n]*(\n|$)', '')
$content = [regex]::Replace($content, '(?m)^(\s*)(dtoverlay=vc4-kms-v3d.*)$', '$1#$2   # disabled for the PiBoy DPI screen')
$content = $content.TrimEnd() + "`n`n" + (Read-LF $blockFile)

if (($content -notmatch '(?m)^enable_dpi_lcd=1') -or
    ($content -notmatch '(?m)^# ====== end of PiBoy DMG block') -or
    ($content -match '(?m)^\s*dtoverlay=vc4-kms-v3d')) {
    throw 'The new file failed its check, config.txt left untouched.'
}

# LF only and no BOM: the Pi firmware reads this file, not Notepad.
$tmp = "$cfg.piboy-new"
[System.IO.File]::WriteAllText($tmp, $content, (New-Object System.Text.UTF8Encoding($false)))
Move-Item -Force $tmp $cfg
Write-Host "  vc4-kms-v3d disabled, PiBoy block for the Pi $Pi written" -ForegroundColor Green
Write-Host 'Done. Eject the card cleanly, then boot the console.' -ForegroundColor Cyan
