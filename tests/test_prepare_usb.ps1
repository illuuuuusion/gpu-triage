<#
Hardware-free integration tests for tools/prepare-usb.ps1.

Run from Windows PowerShell 5.1. A subst drive backed by a temporary directory
stands in for the Ventoy data partition; all downloads are pre-populated in a
temporary cache with their real hashes, so the suite never uses the network.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$SourcePrepare = Join-Path $RepoRoot 'tools\prepare-usb.ps1'
$Failed = 0

function Pass([string]$Name) { Write-Host "ok   $Name" }
function Fail([string]$Name, [string]$Detail = '') {
    Write-Host "FAIL $Name" -ForegroundColor Red
    if ($Detail) { Write-Host "       $Detail" }
    $script:Failed++
}
function Assert-True([string]$Name, [bool]$Value, [string]$Detail = '') {
    if ($Value) { Pass $Name } else { Fail $Name $Detail }
}
function Assert-Contains([string]$Name, [string]$Text, [string]$Needle) {
    Assert-True $Name ($Text.Contains($Needle)) "missing: $Needle`n       output: $Text"
}

function Invoke-Prepare {
    param([string[]]$Parameters)
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:Prepare) + $Parameters
    $text = (& powershell.exe @arguments 2>&1 | Out-String)
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $text }
}

function Find-FreeDriveLetter {
    foreach ($letter in [char[]]'ZYXWVUTSRQPONMLKJIHGFED') {
        if (-not (Test-Path "$letter`:")) { return "$letter`:" }
    }
    throw 'No free drive letter available for the subst fixture.'
}

function Mount-Subst([string]$Path) {
    $drive = Find-FreeDriveLetter
    & subst.exe $drive $Path
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$drive\")) {
        throw "subst failed for $drive -> $Path"
    }
    return $drive
}

$tokens = $null
$parseErrors = $null
# ParseInput also works when this test is launched through a WSL UNC path;
# Windows PowerShell 5.1's ParseFile rejects that otherwise valid path format.
$prepareSource = Get-Content -LiteralPath $SourcePrepare -Raw -Encoding UTF8
[void][System.Management.Automation.Language.Parser]::ParseInput(
    $prepareSource, [ref]$tokens, [ref]$parseErrors)
Assert-True 'prepare-usb.ps1 parses in Windows PowerShell' ($parseErrors.Count -eq 0) `
    (($parseErrors | ForEach-Object Message) -join '; ')

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('gpu-triage-prepare-test-' + [guid]::NewGuid().ToString('N'))
$FixtureRepo = Join-Path $TempRoot 'repo'
$StickDir = Join-Path $TempRoot 'stick'
$EmptyStickDir = Join-Path $TempRoot 'empty-stick'
$CacheDir = Join-Path $TempRoot 'cache'
$BundleSource = Join-Path $TempRoot 'bundle-source'
$Drive = $null
$EmptyDrive = $null

try {
    foreach ($path in @($FixtureRepo, $StickDir, $EmptyStickDir, $CacheDir, $BundleSource)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }

    # Keep the fixture small and isolated from locally generated package/cache
    # files. Copy-Item is used here because robocopy can hang when the test
    # itself was launched from a WSL UNC path; prepare-usb's own robocopy call
    # still runs below against this local fixture and the subst drive.
    foreach ($directory in @('app', 'scripts', 'tools')) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot $directory) -Destination $FixtureRepo -Recurse
    }
    foreach ($file in @('go.sh', 'start.sh', 'README.md')) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot $file) -Destination $FixtureRepo
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureRepo 'offline') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureRepo 'offline\packages') | Out-Null
    $script:Prepare = Join-Path $FixtureRepo 'tools\prepare-usb.ps1'

    $IsoName = 'archlinux-2099.01.01-x86_64.iso'
    $BundleName = 'gpu-triage-bundle-2099.01.01.zip'
    $CachedIso = Join-Path $CacheDir $IsoName
    $CachedBundle = Join-Path $CacheDir $BundleName
    [System.IO.File]::WriteAllText($CachedIso, 'synthetic ISO bytes', [Text.Encoding]::ASCII)

    $BundlePackages = Join-Path $BundleSource 'packages'
    New-Item -ItemType Directory -Force -Path $BundlePackages | Out-Null
    $PackageName = 'synthetic-1_2.0-1-x86_64.pkg.tar.zst'
    $PackagePath = Join-Path $BundlePackages $PackageName
    [System.IO.File]::WriteAllText($PackagePath, 'synthetic package bytes', [Text.Encoding]::ASCII)
    $PackageSha = (Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "ARCHISO_DATE='2099.01.01'"
        "EXPECTED_KERNEL='9.9.9-arch1-1'"
        "BUNDLE_PACKAGES='1'"
    ) | Set-Content -LiteralPath (Join-Path $BundleSource 'manifest.env') -Encoding ASCII
    "$PackageSha  packages/$PackageName" |
        Set-Content -LiteralPath (Join-Path $BundleSource 'SHA256SUMS') -Encoding ASCII
    'base 1.0-1' | Set-Content -LiteralPath (Join-Path $BundleSource 'excluded.txt') -Encoding ASCII
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $BundleSource, $CachedBundle, [System.IO.Compression.CompressionLevel]::NoCompression, $false)

    $ReleasePath = Join-Path $TempRoot 'release.json'
    $Release = [ordered]@{
        schema = 1
        generated = '2099-01-01T00:00:00Z'
        iso_date = '2099.01.01'
        iso_name = $IsoName
        iso_sha256 = (Get-FileHash -LiteralPath $CachedIso -Algorithm SHA256).Hash.ToLowerInvariant()
        iso_size = (Get-Item -LiteralPath $CachedIso).Length
        iso_urls = @("https://invalid.example/$IsoName")
        expected_kernel = '9.9.9-arch1-1'
        release_tag = 'bundle-2099.01.01'
        bundle_name = $BundleName
        bundle_url = "https://invalid.example/$BundleName"
        bundle_sha256 = (Get-FileHash -LiteralPath $CachedBundle -Algorithm SHA256).Hash.ToLowerInvariant()
        bundle_size = (Get-Item -LiteralPath $CachedBundle).Length
        bundle_packages = 1
    }
    $Release | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReleasePath -Encoding UTF8

    $Drive = Mount-Subst $StickDir
    $common = @('-Drive', $Drive, '-CacheDir', $CacheDir, '-ReleasePath', $ReleasePath)

    $first = Invoke-Prepare $common
    Assert-True 'first run succeeds on empty fake stick' ($first.ExitCode -eq 0) $first.Output
    Assert-Contains 'first run reports ready' $first.Output 'USB stick ready.'
    Assert-True 'ISO is copied to fake stick' (Test-Path (Join-Path "$Drive\" $IsoName))
    Assert-True 'bundle package is mirrored to fake stick' `
        (Test-Path (Join-Path "$Drive\gpu-triage\offline\packages" $PackageName))
    Assert-True 'BOOT.txt is written' (Test-Path "$Drive\BOOT.txt")
    Assert-True 'verification state is written' (Test-Path "$Drive\.gpu-triage-state.json")

    $second = Invoke-Prepare $common
    Assert-True 'second run succeeds' ($second.ExitCode -eq 0) $second.Output
    Assert-Contains 'second run trusts matching ISO state' $second.Output 'ISO already verified on this stick'
    Assert-Contains 'second run reuses unpacked bundle' $second.Output 'Bundle already unpacked'
    Assert-True 'second run performs no download' (-not $second.Output.Contains('GET https://')) $second.Output

    $doctor = Invoke-Prepare ($common + '-Check')
    Assert-True '-Check accepts healthy fake stick' ($doctor.ExitCode -eq 0) $doctor.Output
    Assert-Contains '-Check reports ready' $doctor.Output 'Ready to boot.'

    $StickIso = Join-Path "$Drive\" $IsoName
    Remove-Item -LiteralPath $StickIso -Force
    $result = Invoke-Prepare ($common + '-Check')
    Assert-True '-Check rejects missing ISO' ($result.ExitCode -eq 1)
    Assert-Contains 'missing ISO diagnostic is explicit' $result.Output 'ISO missing:'
    Copy-Item -LiteralPath $CachedIso -Destination $StickIso

    [System.IO.File]::WriteAllText($StickIso, 'wrong size', [Text.Encoding]::ASCII)
    $result = Invoke-Prepare ($common + '-Check')
    Assert-Contains 'wrong ISO size diagnostic is explicit' $result.Output 'ISO has the wrong size:'
    Copy-Item -LiteralPath $CachedIso -Destination $StickIso -Force

    $bytes = [System.IO.File]::ReadAllBytes($StickIso)
    $bytes[0] = $bytes[0] -bxor 1
    [System.IO.File]::WriteAllBytes($StickIso, $bytes)
    $result = Invoke-Prepare ($common + '-Check')
    Assert-Contains 'wrong ISO hash diagnostic is explicit' $result.Output 'ISO sha256 does not match release.json'
    Copy-Item -LiteralPath $CachedIso -Destination $StickIso -Force

    $StickPackage = Join-Path "$Drive\gpu-triage\offline\packages" $PackageName
    Remove-Item -LiteralPath $StickPackage -Force
    $result = Invoke-Prepare ($common + '-Check')
    Assert-Contains 'missing package diagnostic is explicit' $result.Output 'package files are missing'
    Copy-Item -LiteralPath $PackagePath -Destination $StickPackage

    $StickManifest = "$Drive\gpu-triage\offline\manifest.env"
    $manifestBackup = Get-Content -LiteralPath $StickManifest -Raw
    $manifestBackup.Replace("ARCHISO_DATE='2099.01.01'", "ARCHISO_DATE='2098.12.01'") |
        Set-Content -LiteralPath $StickManifest -Encoding ASCII
    $result = Invoke-Prepare ($common + '-Check')
    Assert-Contains 'wrong bundle ISO diagnostic is explicit' $result.Output 'built for ISO 2098.12.01'
    $manifestBackup | Set-Content -LiteralPath $StickManifest -Encoding ASCII

    $EmptyDrive = Mount-Subst $EmptyStickDir
    $result = Invoke-Prepare @('-Drive', $EmptyDrive, '-CacheDir', $CacheDir, '-ReleasePath', $ReleasePath, '-Check')
    Assert-True '-Check rejects empty fake stick' ($result.ExitCode -eq 1)
    Assert-Contains 'empty stick reports missing repository' $result.Output 'No gpu-triage repository on the stick'

    $FutureRelease = Join-Path $TempRoot 'future-release.json'
    $future = [ordered]@{}
    foreach ($entry in $Release.GetEnumerator()) { $future[$entry.Key] = $entry.Value }
    $future.schema = 2
    $future | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $FutureRelease -Encoding UTF8
    $result = Invoke-Prepare @('-Drive', $Drive, '-CacheDir', $CacheDir, '-ReleasePath', $FutureRelease, '-Check')
    Assert-True 'future release schema is rejected' ($result.ExitCode -eq 1)
    Assert-Contains 'future schema asks for repository update' $result.Output 'Update the repository'

    $result = Invoke-Prepare @('-Drive', $Drive, '-CacheDir', $CacheDir,
        '-ReleasePath', (Join-Path $TempRoot 'missing-release.json'), '-Check')
    Assert-True 'missing release.json is rejected' ($result.ExitCode -eq 1)
    Assert-Contains 'missing release.json names manual fallback' $result.Output 'take the manual route'

    $result = Invoke-Prepare ($common + @('-Check', '-InstallVentoy'))
    Assert-True '-Check and -InstallVentoy are rejected together' ($result.ExitCode -eq 1)
    Assert-Contains 'mutually exclusive switches are explained' $result.Output 'mutually exclusive'
} finally {
    if ($EmptyDrive) { & subst.exe $EmptyDrive /D | Out-Null }
    if ($Drive) { & subst.exe $Drive /D | Out-Null }
    if (Test-Path -LiteralPath $TempRoot) { Remove-Item -LiteralPath $TempRoot -Recurse -Force }
}

if ($Failed -eq 0) {
    Write-Host 'prepare-usb.ps1 tests: PASS'
} else {
    Write-Host 'prepare-usb.ps1 tests: FAIL' -ForegroundColor Red
}
exit $Failed
