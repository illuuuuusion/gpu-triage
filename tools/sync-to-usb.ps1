param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[A-Za-z]:$')]
    [string]$Drive,

    [Parameter(Mandatory=$false)]
    [string]$IsoPath
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Target = Join-Path "$Drive\" 'gpu-triage'

if (-not (Test-Path "$Drive\")) {
    throw "Drive $Drive does not exist."
}

Write-Host "Syncing repository to $Target"
New-Item -ItemType Directory -Force -Path $Target | Out-Null

# /MIR keeps the USB copy aligned with the repo. Excluded are .git, runtime
# outputs, and the two build artifacts that must never reach the stick: the
# package download cache holds the full dependency closure and 'dist' holds a
# second, zipped copy of the very packages already synced under offline/packages.
$null = robocopy $Repo $Target /MIR /XD '.git' 'reports' '.dlcache' 'dist' /XF '*.iso' /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Target 'reports') | Out-Null

if ($IsoPath) {
    $Iso = Resolve-Path $IsoPath
    Write-Host "Copying Arch ISO: $($Iso.Path)"
    Copy-Item -Force $Iso.Path (Join-Path "$Drive\" (Split-Path -Leaf $Iso.Path))
}

Write-Host ''
Write-Host 'Repository synced.'
Write-Host 'Boot the Arch ISO through Ventoy, then type this one line:'
Write-Host '  m=/mnt/v; mkdir -p $m; mount /dev/disk/by-label/Ventoy $m; bash $m/gpu-triage/go.sh list'
Write-Host ''
Write-Host 'go.sh mounts and remounts as needed and then hands over to start.sh.'
Write-Host 'prepare-usb.ps1 writes the same line into BOOT.txt on the stick root.'
