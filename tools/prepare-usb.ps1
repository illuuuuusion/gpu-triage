<#
.SYNOPSIS
    Prepares the gpu-triage USB stick with a single command on Windows.

.DESCRIPTION
    Reads offline/release.json - the one file that pins the Arch ISO, the
    kernel and the offline package bundle to each other - then finds the Ventoy
    stick, fetches both parts hash-verified, mirrors the repository onto the
    stick and writes BOOT.txt with the one command needed after booting.

    No Linux environment is involved. Every download is verified against the
    hash in release.json and cached, so a second run downloads nothing and is
    the fast way to push a code change onto the stick.

.PARAMETER Drive
    Use this drive instead of searching for a volume labelled "Ventoy".

.PARAMETER Check
    Doctor mode: verify an existing stick and write nothing. Exit code 0 means
    the stick is ready to boot, 1 means it is not.

.PARAMETER InstallVentoy
    Install Ventoy onto a USB disk first. Destructive, opt-in, and asks for a
    typed confirmation naming the disk.

.PARAMETER CacheDir
    Download cache. Default: %LOCALAPPDATA%\gpu-triage\cache

.PARAMETER ReleasePath
    Alternate release.json, primarily for isolated regression tests. By
    default offline\release.json in this repository is used.

.PARAMETER Force
    Re-verify and re-fetch even where the recorded state says everything is
    already in place.

.PARAMETER MaxDiskSizeGB
    Upper size limit for a disk that -InstallVentoy is allowed to erase.

.EXAMPLE
    .\tools\prepare-usb.ps1

.EXAMPLE
    .\tools\prepare-usb.ps1 -Check
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z]:$')]
    [string]$Drive,

    [switch]$Check,

    [switch]$InstallVentoy,

    [string]$CacheDir,

    [string]$ReleasePath,

    [switch]$Force,

    [ValidateRange(8, 4096)]
    [int]$MaxDiskSizeGB = 256
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# Every failure below is a sentence aimed at the person holding the stick.
# Wrapping the PowerShell exception report around it would bury the sentence,
# so the message is printed on its own; -Verbose adds the stack for debugging.
trap {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($VerbosePreference -ne 'SilentlyContinue') { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    exit 1
}

# TLS 1.2 is not the default in Windows PowerShell 5.1; GitHub and the Arch
# mirrors refuse anything older.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$RepoRoot      = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $ReleasePath) {
    $ReleasePath = Join-Path $RepoRoot 'offline\release.json'
} else {
    $ReleasePath = [System.IO.Path]::GetFullPath($ReleasePath)
}
$OfflineDir    = Join-Path $RepoRoot 'offline'
$SyncScript    = Join-Path $PSScriptRoot 'sync-to-usb.ps1'
$VentoyPin     = Join-Path $PSScriptRoot 'ventoy-release.json'
$SchemaVersion = 1

# Directory names robocopy /MIR in sync-to-usb.ps1 keeps off the stick. The
# space estimate has to use the same list or it would ask for gigabytes that
# are never copied.
$SyncExcludedDirs = @('.git', 'reports', '.dlcache', 'dist')

if (-not $CacheDir) {
    $CacheDir = Join-Path $env:LOCALAPPDATA 'gpu-triage\cache'
}

$script:Findings = @()

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

function Write-Step { param([string]$Text) Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Info { param([string]$Text) Write-Host "       $Text" -ForegroundColor DarkGray }

function Add-Finding {
    param(
        [ValidateSet('OK', 'WARN', 'FAIL')][string]$State,
        [string]$Text
    )
    $script:Findings += [pscustomobject]@{ State = $State; Text = $Text }
    $colour = switch ($State) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } }
    Write-Host ('[{0,4}] {1}' -f $State, $Text) -ForegroundColor $colour
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

# --------------------------------------------------------------------------
# release.json
# --------------------------------------------------------------------------

function Get-Release {
    if (-not (Test-Path -LiteralPath $ReleasePath)) {
        throw @"
offline/release.json is missing - there is nothing to prepare the stick from.

That file is written by the bundle workflow (.github/workflows/bundle.yml),
which publishes the offline bundle as a release asset and opens a pull request
adding release.json. Pull the branch that contains it, or take the manual route
described in README.md (build the bundle yourself, then use sync-to-usb.ps1).
"@
    }

    try {
        $release = Get-Content -LiteralPath $ReleasePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "offline/release.json is not valid JSON: $($_.Exception.Message)"
    }

    # Same field list release_meta.py validates on write. Checking it again here
    # keeps a hand-edited file from failing halfway through a 1.6 GB download.
    $required = @(
        'schema', 'iso_date', 'iso_name', 'iso_sha256', 'iso_size', 'iso_urls',
        'expected_kernel', 'release_tag', 'bundle_name', 'bundle_url',
        'bundle_sha256', 'bundle_size', 'bundle_packages'
    )
    $missing = @($required | Where-Object { -not ($release.PSObject.Properties.Name -contains $_) })
    if ($missing.Count -gt 0) {
        throw "offline/release.json is incomplete, missing: $($missing -join ', ')"
    }

    if ($release.schema -gt $SchemaVersion) {
        throw @"
offline/release.json uses schema $($release.schema), this script understands $SchemaVersion.
Update the repository (git pull) so prepare-usb.ps1 matches the release file.
"@
    }
    if ($release.schema -ne $SchemaVersion) {
        throw "offline/release.json uses unknown schema $($release.schema)."
    }
    foreach ($field in @('iso_sha256', 'bundle_sha256')) {
        if ($release.$field -notmatch '^[0-9a-f]{64}$') {
            throw "offline/release.json: $field is not a lowercase sha256 digest."
        }
    }

    return $release
}

# --------------------------------------------------------------------------
# Finding the stick
# --------------------------------------------------------------------------

function Get-VentoyVolume {
    # Get-Volume comes from the Storage module. Where it is unavailable the CIM
    # class carries the same two facts this needs: label and drive letter.
    $found = @()
    try {
        $found = @(Get-Volume -FileSystemLabel 'Ventoy' -ErrorAction Stop |
                   Where-Object { $_.DriveLetter } |
                   ForEach-Object { [string]$_.DriveLetter + ':' })
    } catch {
        $found = @(Get-CimInstance -ClassName Win32_LogicalDisk -ErrorAction SilentlyContinue |
                   Where-Object { $_.VolumeName -eq 'Ventoy' -and $_.DeviceID } |
                   ForEach-Object { $_.DeviceID })
    }
    return $found
}

function Get-DriveDescription {
    param([string]$DriveLetter)
    try {
        $disk = Get-Partition -DriveLetter $DriveLetter.TrimEnd(':') -ErrorAction Stop |
                Get-Disk -ErrorAction Stop
        return ('{0} ({1}, {2})' -f $disk.FriendlyName, (Format-Size $disk.Size), $disk.BusType)
    } catch {
        return 'unknown model'
    }
}

function Resolve-TargetDrive {
    if ($Drive) {
        $root = "$Drive\"
        if (-not (Test-Path -LiteralPath $root)) { throw "Drive $Drive does not exist." }
        $label = ''
        try { $label = (Get-Volume -DriveLetter $Drive.TrimEnd(':') -ErrorAction Stop).FileSystemLabel } catch { }
        if ($label -and $label -ne 'Ventoy') {
            Write-Warning "$Drive is labelled '$label', not 'Ventoy'. Continuing because -Drive was given explicitly."
        }
        return $Drive
    }

    $candidates = @(Get-VentoyVolume)

    if ($candidates.Count -eq 0) {
        throw @"
No volume labelled 'Ventoy' found.

Plug the prepared stick in, or pass -Drive E: for a stick with a different
label. If the stick has no Ventoy on it yet, run

    .\tools\prepare-usb.ps1 -InstallVentoy

which erases a USB disk after asking for a typed confirmation.
"@
    }

    if ($candidates.Count -eq 1) { return $candidates[0] }

    Write-Host ''
    Write-Host 'Several volumes are labelled Ventoy:' -ForegroundColor Yellow
    for ($i = 0; $i -lt $candidates.Count; $i++) {
        Write-Host ('  [{0}] {1}  {2}' -f ($i + 1), $candidates[$i], (Get-DriveDescription $candidates[$i]))
    }
    while ($true) {
        $answer = Read-Host "Which one? (1-$($candidates.Count), or 'q' to abort)"
        if ($answer -eq 'q') { throw 'Aborted - no drive selected.' }
        $index = 0
        if ([int]::TryParse($answer, [ref]$index) -and $index -ge 1 -and $index -le $candidates.Count) {
            return $candidates[$index - 1]
        }
    }
}

# --------------------------------------------------------------------------
# Hashes, state, sizes
# --------------------------------------------------------------------------

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# Hashing 1.6 GB off a USB stick takes minutes, and the plan's promise is that a
# second run is the *fast* way to push a code change. The state file records
# which file was hashed, so an unchanged ISO is recognised by name, size and
# write time. It lives on the stick root, outside the directory sync-to-usb.ps1
# mirrors with /MIR, which would otherwise delete it on every run.
function Get-StatePath { param([string]$DriveLetter) return (Join-Path "$DriveLetter\" '.gpu-triage-state.json') }

function Read-State {
    param([string]$DriveLetter)
    $path = Get-StatePath $DriveLetter
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

function Write-State {
    param([string]$DriveLetter, [hashtable]$State)
    $path = Get-StatePath $DriveLetter
    ($State | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $path -Encoding UTF8
    try { (Get-Item -LiteralPath $path -Force).Attributes = 'Hidden' } catch { }
}

function Test-VerifiedByState {
    param([string]$Path, $StateEntry, [string]$ExpectedSha256)
    if ($Force -or -not $StateEntry) { return $false }
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $file = Get-Item -LiteralPath $Path -Force
    return ($StateEntry.sha256 -eq $ExpectedSha256 -and
            $StateEntry.size -eq $file.Length -and
            $StateEntry.mtime -eq $file.LastWriteTimeUtc.ToString('o'))
}

function New-StateEntry {
    param([string]$Path, [string]$Sha256)
    $file = Get-Item -LiteralPath $Path -Force
    return @{
        name     = $file.Name
        size     = $file.Length
        mtime    = $file.LastWriteTimeUtc.ToString('o')
        sha256   = $Sha256
        verified = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Get-PayloadSize {
    param([string]$Root, [string[]]$ExcludeDirs)
    $total = 0L
    $stack = New-Object System.Collections.Stack
    $stack.Push($Root)
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($sub in [System.IO.Directory]::GetDirectories($dir)) {
            if ($ExcludeDirs -contains ([System.IO.Path]::GetFileName($sub))) { continue }
            $stack.Push($sub)
        }
        foreach ($file in [System.IO.Directory]::GetFiles($dir)) {
            if ($file.EndsWith('.iso', [StringComparison]::OrdinalIgnoreCase)) { continue }
            $total += (New-Object System.IO.FileInfo $file).Length
        }
    }
    return $total
}

function Get-FreeSpace {
    param([string]$Path)
    $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $Path).Path)
    return (New-Object System.IO.DriveInfo $root).AvailableFreeSpace
}

function Assert-FreeSpace {
    param([string]$Path, [long]$Needed, [string]$What)
    if ($Needed -le 0) { return }
    $free = Get-FreeSpace $Path
    if ($free -lt $Needed) {
        throw ("Not enough space for {0}: {1} needed on {2}, {3} free." -f `
               $What, (Format-Size $Needed), $Path, (Format-Size $free))
    }
    Write-Info ('{0}: {1} needed, {2} free on {3}' -f $What, (Format-Size $Needed), (Format-Size $free), $Path)
}

# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

function Invoke-VerifiedDownload {
    <#
        Downloads the first URL that works into $Destination and only keeps the
        file if it hashes to $ExpectedSha256. A partial download stays behind as
        .part and is overwritten on the next attempt rather than mistaken for a
        finished file.
    #>
    param(
        [string[]]$Urls,
        [string]$Destination,
        [string]$ExpectedSha256,
        [long]$ExpectedSize = 0
    )

    try { Add-Type -AssemblyName System.Net.Http -ErrorAction Stop } catch { }

    $partial = "$Destination.part"
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

    $errors = @()
    foreach ($url in $Urls) {
        Write-Info "GET $url"
        $client = $null; $response = $null; $stream = $null; $output = $null
        try {
            $client = New-Object System.Net.Http.HttpClient
            $client.Timeout = [TimeSpan]::FromHours(3)
            $response = $client.GetAsync($url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).
                        GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                throw "HTTP $([int]$response.StatusCode) $($response.ReasonPhrase)"
            }

            $total = 0L
            if ($response.Content.Headers.ContentLength) { $total = [long]$response.Content.Headers.ContentLength }
            if ($ExpectedSize -gt 0 -and $total -gt 0 -and $total -ne $ExpectedSize) {
                throw "server offers $total bytes, release.json expects $ExpectedSize"
            }

            $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $output = [System.IO.File]::Create($partial)
            $buffer = New-Object byte[] (1MB)
            $done = 0L
            $lastReport = -1
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $output.Write($buffer, 0, $read)
                $done += $read
                if ($total -gt 0) {
                    $percent = [int](100 * $done / $total)
                    if ($percent -ne $lastReport) {
                        $lastReport = $percent
                        Write-Progress -Activity "Downloading $(Split-Path -Leaf $Destination)" `
                            -Status ('{0} of {1}' -f (Format-Size $done), (Format-Size $total)) `
                            -PercentComplete $percent
                    }
                }
            }
            $output.Dispose(); $output = $null
            Write-Progress -Activity "Downloading $(Split-Path -Leaf $Destination)" -Completed

            $actual = Get-Sha256 $partial
            if ($actual -ne $ExpectedSha256) {
                throw "sha256 mismatch (expected $ExpectedSha256, got $actual)"
            }
            Move-Item -LiteralPath $partial -Destination $Destination -Force
            Write-Info "Verified $(Split-Path -Leaf $Destination) ($(Format-Size (Get-Item -LiteralPath $Destination).Length))"
            return
        } catch {
            $errors += "$url : $($_.Exception.Message)"
            Write-Warning "Download failed: $($_.Exception.Message)"
        } finally {
            if ($output) { $output.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($response) { $response.Dispose() }
            if ($client) { $client.Dispose() }
        }
    }

    throw ("Could not download $(Split-Path -Leaf $Destination):`n  " + ($errors -join "`n  "))
}

function Get-CachedFile {
    <#
        Returns the path of a hash-verified copy in the cache, downloading it
        only if it is not there yet. This is what makes a second run free.
    #>
    param([string]$Name, [string[]]$Urls, [string]$Sha256, [long]$Size)

    $path = Join-Path $CacheDir $Name
    if (Test-Path -LiteralPath $path) {
        if ($Force) {
            # Not deleted here: the download writes to .part and only replaces
            # the cached file once the new bytes hash correctly, so a -Force run
            # that loses its network connection still leaves a usable cache.
            Write-Info "-Force: fetching $Name again"
        } else {
            $file = Get-Item -LiteralPath $path
            if ($file.Length -eq $Size) {
                Write-Info "Cached: $Name - verifying hash"
                if ((Get-Sha256 $path) -eq $Sha256) { return $path }
                Write-Warning "Cached $Name has the wrong hash and is discarded."
            } else {
                Write-Warning "Cached $Name has the wrong size and is discarded."
            }
            Remove-Item -LiteralPath $path -Force
        }
    }

    Invoke-VerifiedDownload -Urls $Urls -Destination $path -ExpectedSha256 $Sha256 -ExpectedSize $Size
    return $path
}

# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------

function Read-Manifest {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match "^([A-Z_][A-Z0-9_]*)='(.*)'$") { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

function Get-SumsEntries {
    # The relative paths SHA256SUMS covers. bootstrap.sh derives its install
    # list from exactly these lines, so "complete" means: every one of them is
    # on the stick.
    param([string]$Path)
    # Plain 'return $entries' on purpose: a ',$entries' wrapper would reach the
    # caller as a single nested array, and every call site counts the result.
    $entries = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $entries }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*[0-9a-fA-F]{64}\s+\*?(\S.*)$') { $entries += $Matches[1].Trim() }
    }
    return $entries
}

function Test-BundleComplete {
    <#
        Everything about an unpacked bundle that can be established without
        re-hashing 372 MB: it belongs to this ISO, it is complete, and every
        package file SHA256SUMS names is actually there. bootstrap.sh verifies
        the hashes again on the diagnostic PC before it installs anything.
    #>
    param([string]$OfflinePath, $Release, [ref]$Reason)

    $manifestPath = Join-Path $OfflinePath 'manifest.env'
    $sumsPath = Join-Path $OfflinePath 'SHA256SUMS'
    $profileSumsPath = Join-Path $OfflinePath 'PROFILE-SHA256SUMS'

    if (-not (Test-Path -LiteralPath $manifestPath)) { $Reason.Value = 'manifest.env missing'; return $false }
    if (-not (Test-Path -LiteralPath $sumsPath)) { $Reason.Value = 'SHA256SUMS missing'; return $false }
    $profileFiles = @('safe-runtime.files', 'driver-bound-runtime.files')
    $presentProfiles = @($profileFiles | Where-Object {
        Test-Path -LiteralPath (Join-Path $OfflinePath "profiles\$_")
    })
    $hasProfileSums = Test-Path -LiteralPath $profileSumsPath
    if (($presentProfiles.Count -gt 0 -or $hasProfileSums) -and
        ($presentProfiles.Count -ne $profileFiles.Count -or -not $hasProfileSums)) {
        $Reason.Value = 'runtime profile metadata is only partially present'
        return $false
    }

    $manifest = Read-Manifest $manifestPath
    if ($manifest['ARCHISO_DATE'] -ne $Release.iso_date) {
        $Reason.Value = "built for ISO $($manifest['ARCHISO_DATE']), release.json pins $($Release.iso_date)"
        return $false
    }
    if ($manifest['EXPECTED_KERNEL'] -ne $Release.expected_kernel) {
        $Reason.Value = "expects kernel $($manifest['EXPECTED_KERNEL']), release.json pins $($Release.expected_kernel)"
        return $false
    }

    $entries = @(Get-SumsEntries $sumsPath)
    if ($entries.Count -ne $Release.bundle_packages) {
        $Reason.Value = "SHA256SUMS lists $($entries.Count) packages, release.json says $($Release.bundle_packages)"
        return $false
    }
    $absent = @($entries | Where-Object { -not (Test-Path -LiteralPath (Join-Path $OfflinePath $_)) })
    if ($absent.Count -gt 0) {
        $Reason.Value = "$($absent.Count) of $($entries.Count) package files are missing (e.g. $($absent[0]))"
        return $false
    }

    $profileNote = if ($hasProfileSums) { ', role profiles present' } else { ', legacy bundle (safe tools must come from ISO)' }
    $Reason.Value = "$($entries.Count) packages, ISO $($manifest['ARCHISO_DATE']), kernel $($manifest['EXPECTED_KERNEL'])$profileNote"
    return $true
}

function Expand-Bundle {
    <#
        Unpacks the release asset into the repository's own offline/ directory
        rather than straight onto the stick.

        The plan put the repository mirror after the bundle, which cannot work:
        sync-to-usb.ps1 mirrors with robocopy /MIR, and /MIR deletes whatever it
        does not find in the source - it would wipe the packages again moments
        after they were unpacked. Unpacking into the source instead means the
        mirror carries the bundle along and stays the single writer of the
        stick's gpu-triage directory.
    #>
    param([string]$ZipPath, [string]$Target)

    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $packages = Join-Path $Target 'packages'
    if (Test-Path -LiteralPath $packages) {
        Get-ChildItem -LiteralPath $packages -File -Force | Where-Object { $_.Name -ne '.gitkeep' } |
            Remove-Item -Force
    }
    foreach ($stale in @('manifest.env', 'SHA256SUMS', 'PROFILE-SHA256SUMS', 'excluded.txt')) {
        $path = Join-Path $Target $stale
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    $profiles = Join-Path $Target 'profiles'
    if (Test-Path -LiteralPath $profiles) {
        Get-ChildItem -LiteralPath $profiles -File -Filter '*.files' | Remove-Item -Force
    }

    $targetFull = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\') + '\'
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $count = 0
        foreach ($entry in $zip.Entries) {
            $relative = $entry.FullName -replace '/', '\'
            $destination = [System.IO.Path]::GetFullPath((Join-Path $targetFull $relative))
            # A zip entry may not escape the directory it is unpacked into.
            if (-not $destination.StartsWith($targetFull, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Bundle contains an entry pointing outside offline/: $($entry.FullName)"
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Force -Path $destination | Out-Null
                continue
            }
            $parent = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $true)
            $count++
        }
        Write-Info "Unpacked $count files into $Target"
    } finally {
        $zip.Dispose()
    }
}

# --------------------------------------------------------------------------
# BOOT.txt
# --------------------------------------------------------------------------

function Write-BootTxt {
    param([string]$DriveLetter, $Release)

    $oneLiner = 'd=$(readlink -f /dev/disk/by-label/Ventoy); m=/mnt/v; mkdir -p $m; mount /dev/mapper/${d##*/} $m || mount "$d" $m; bash $m/gpu-triage/go.sh list'
    $lines = @(
        'gpu-triage - what to type after booting'
        '======================================='
        ''
        "1. Boot this stick and pick $($Release.iso_name) in the Ventoy menu."
        '2. In the root shell, type this single line:'
        ''
        "   $oneLiner"
        ''
        '   It mounts the stick and lists the GPUs it can see. To run a diagnosis:'
        ''
        '   bash $m/gpu-triage/go.sh triage --gpu 0000:03:00.0 --preflight-only'
        ''
        'Reports are written to gpu-triage/reports on this stick.'
        ''
        'This stick was prepared for exactly one ISO:'
        ''
        "   ISO              $($Release.iso_name)"
        "   Expected kernel  $($Release.expected_kernel)"
        "   Bundle           $($Release.bundle_name) ($($Release.bundle_packages) packages)"
        "   Prepared         $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')) UTC"
        ''
        'Booting a different Arch ISO stops the bootstrap with exit code 3 rather'
        'than installing a driver stack built for another kernel.'
    )
    $path = Join-Path "$DriveLetter\" 'BOOT.txt'
    Set-Content -LiteralPath $path -Value $lines -Encoding ASCII
    return $path
}

function Write-SafeBootTxt {
    param([string]$DriveLetter)
    $lines = @(
        'gpu-triage - SAFE BOOT IS REQUIRED BEFORE PREFLIGHT'
        '==================================================='
        ''
        'Edit the Arch ISO kernel command line in the Ventoy boot menu.'
        ''
        'AMD DUT, Intel/NVIDIA display GPU:'
        '  module_blacklist=amdgpu,radeon'
        ''
        'NVIDIA DUT, Intel/AMD display GPU:'
        '  module_blacklist=nouveau,nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm'
        ''
        'Do not use the global blacklist when the display GPU has the same vendor'
        'as the DUT. That setup remains BLOCKED: SAFE_BOOT_NOT_PROVEN until the'
        'BDF-specific initramfs guard has passed real-hardware validation.'
        ''
        'A command typed after boot cannot prevent a hard lock during udev coldplug.'
        'See gpu-triage/docs/SAFE-BOOT.md for the exact procedure and limitations.'
    )
    $path = Join-Path "$DriveLetter\" 'SAFE-BOOT.txt'
    Set-Content -LiteralPath $path -Value $lines -Encoding ASCII
    return $path
}

# --------------------------------------------------------------------------
# Doctor mode
# --------------------------------------------------------------------------

function Invoke-Check {
    # -SkipIsoHash is only ever passed by the preparation run, which verified
    # the very same bytes minutes earlier. -Check always hashes: a doctor that
    # trusts a recorded result is not a doctor.
    param([string]$DriveLetter, $Release, [switch]$SkipIsoHash)

    Write-Step "Checking $DriveLetter against release.json ($($Release.iso_date))"

    Add-Finding OK "Stick found: $DriveLetter - $(Get-DriveDescription $DriveLetter)"

    # --- ISO
    $isoPath = Join-Path "$DriveLetter\" $Release.iso_name
    if (-not (Test-Path -LiteralPath $isoPath)) {
        Add-Finding FAIL "ISO missing: $($Release.iso_name)"
    } else {
        $iso = Get-Item -LiteralPath $isoPath
        if ($iso.Length -ne $Release.iso_size) {
            Add-Finding FAIL ("ISO has the wrong size: {0} instead of {1}" -f `
                              (Format-Size $iso.Length), (Format-Size $Release.iso_size))
        } elseif ($SkipIsoHash) {
            Add-Finding OK "ISO present, sha256 verified earlier in this run: $($Release.iso_name)"
        } else {
            Write-Info "Hashing $($Release.iso_name) ($(Format-Size $iso.Length)) - this takes a moment"
            if ((Get-Sha256 $isoPath) -eq $Release.iso_sha256) {
                Add-Finding OK "ISO present and sha256 correct: $($Release.iso_name)"
            } else {
                Add-Finding FAIL "ISO sha256 does not match release.json: $($Release.iso_name)"
            }
        }
    }

    # --- repository and bundle on the stick
    $stickRepo = Join-Path "$DriveLetter\" 'gpu-triage'
    if (-not (Test-Path -LiteralPath (Join-Path $stickRepo 'start.sh'))) {
        Add-Finding FAIL "No gpu-triage repository on the stick ($stickRepo)"
    } else {
        Add-Finding OK "Repository present: $stickRepo"

        foreach ($entry in @('go.sh', 'scripts\bootstrap.sh', 'app\gpu_diag.py')) {
            if (-not (Test-Path -LiteralPath (Join-Path $stickRepo $entry))) {
                Add-Finding FAIL "Missing on the stick: gpu-triage\$entry"
            }
        }

        $reason = ''
        if (Test-BundleComplete -OfflinePath (Join-Path $stickRepo 'offline') -Release $Release -Reason ([ref]$reason)) {
            Add-Finding OK "Offline bundle complete: $reason"
        } else {
            Add-Finding FAIL "Offline bundle unusable: $reason"
        }
    }

    # --- reports directory
    $reports = Join-Path $stickRepo 'reports'
    if (-not (Test-Path -LiteralPath $reports)) {
        Add-Finding WARN "reports\ does not exist yet - start.sh creates it on the diagnostic PC"
    } else {
        $probe = Join-Path $reports ('.write-probe-' + [System.Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            Set-Content -LiteralPath $probe -Value 'probe' -Encoding ASCII
            Remove-Item -LiteralPath $probe -Force
            Add-Finding OK 'reports\ is writable'
        } catch {
            Add-Finding FAIL "reports\ is not writable: $($_.Exception.Message)"
        }
    }

    # --- BOOT.txt
    $bootPath = Join-Path "$DriveLetter\" 'BOOT.txt'
    if (Test-Path -LiteralPath $bootPath) {
        $bootContent = Get-Content -LiteralPath $bootPath -Raw
        $mapperAt = $bootContent.IndexOf('/dev/mapper/${d##*/}')
        $rawAt = $bootContent.IndexOf('mount "$d"')
        if ($mapperAt -ge 0 -and $rawAt -gt $mapperAt -and $bootContent.Contains('readlink -f /dev/disk/by-label/Ventoy')) {
            Add-Finding OK 'BOOT.txt contains the mapper-first mount path'
        } else {
            Add-Finding FAIL 'BOOT.txt exists but does not try the label-derived mapper node before the raw partition'
        }
    } else {
        Add-Finding WARN 'BOOT.txt missing - run prepare-usb.ps1 without -Check to write it'
    }
    $safeBootPath = Join-Path "$DriveLetter\" 'SAFE-BOOT.txt'
    if ((Test-Path -LiteralPath $safeBootPath) -and
        (Get-Content -LiteralPath $safeBootPath -Raw).Contains('module_blacklist=amdgpu,radeon')) {
        Add-Finding OK 'SAFE-BOOT.txt contains the AMD safe-boot profile'
    } else {
        Add-Finding FAIL 'SAFE-BOOT.txt missing or incomplete'
    }
}

# --------------------------------------------------------------------------
# Ventoy installation (opt-in, destructive)
# --------------------------------------------------------------------------

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-VentoyOnDisk {
    Write-Step 'Installing Ventoy (destructive)'

    if (-not (Test-Administrator)) {
        throw 'Installing Ventoy needs an elevated PowerShell (Run as administrator).'
    }
    if (-not (Test-Path -LiteralPath $VentoyPin)) {
        throw "Missing $VentoyPin - the pinned Ventoy version and hash live there."
    }
    $pin = Get-Content -LiteralPath $VentoyPin -Raw -Encoding UTF8 | ConvertFrom-Json

    # --- pick the disk
    $disks = @(Get-Disk -ErrorAction Stop | Where-Object {
        $_.BusType -eq 'USB' -and -not $_.IsBoot -and -not $_.IsSystem
    })
    if ($disks.Count -eq 0) {
        throw 'No non-system USB disk found. Ventoy is only installed onto USB media.'
    }

    $tooBig = @($disks | Where-Object { $_.Size -gt ($MaxDiskSizeGB * 1GB) })
    foreach ($big in $tooBig) {
        Write-Warning ("Skipping disk {0} ({1}, {2}) - larger than the {3} GB safety limit. Raise -MaxDiskSizeGB to include it." -f `
                       $big.Number, $big.FriendlyName, (Format-Size $big.Size), $MaxDiskSizeGB)
    }
    $disks = @($disks | Where-Object { $_.Size -le ($MaxDiskSizeGB * 1GB) })
    if ($disks.Count -eq 0) {
        throw "Every USB disk found is larger than the $MaxDiskSizeGB GB safety limit."
    }

    Write-Host ''
    Write-Host 'USB disks that may be erased:' -ForegroundColor Yellow
    foreach ($disk in $disks) {
        Write-Host ('  Disk {0}  {1}  {2}  serial {3}' -f `
                    $disk.Number, $disk.FriendlyName, (Format-Size $disk.Size),
                    ($(if ($disk.SerialNumber) { $disk.SerialNumber.Trim() } else { 'unknown' })))
    }

    $target = $null
    if ($disks.Count -eq 1) {
        $target = $disks[0]
    } else {
        while (-not $target) {
            $answer = Read-Host "Disk number to erase (or 'q' to abort)"
            if ($answer -eq 'q') { throw 'Aborted - no disk selected.' }
            $target = $disks | Where-Object { $_.Number.ToString() -eq $answer } | Select-Object -First 1
        }
    }

    # --- typed confirmation, deliberately not a [y/N]
    $serial = if ($target.SerialNumber) { $target.SerialNumber.Trim() } else { 'unknown' }
    $phrase = "ERASE DISK $($target.Number)"
    Write-Host ''
    Write-Host 'About to erase, completely and irreversibly:' -ForegroundColor Red
    Write-Host ("    Disk    {0}" -f $target.Number)
    Write-Host ("    Model   {0}" -f $target.FriendlyName)
    Write-Host ("    Serial  {0}" -f $serial)
    Write-Host ("    Size    {0}" -f (Format-Size $target.Size))
    Write-Host ("    Bus     {0}" -f $target.BusType)
    Write-Host ''
    $typed = Read-Host "Type exactly '$phrase' to continue"
    if ($typed -cne $phrase) { throw 'Aborted - confirmation did not match.' }

    # --- fetch and verify Ventoy
    $zip = Get-CachedFile -Name $pin.asset -Urls @($pin.url) -Sha256 $pin.sha256 -Size ([long]$pin.size)
    $extractRoot = Join-Path $CacheDir ("ventoy-" + $pin.version)
    if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $extractRoot)

    $exe = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter 'Ventoy2Disk.exe' |
           Select-Object -First 1
    if (-not $exe) { throw "Ventoy2Disk.exe not found in $($pin.asset)." }

    # Ventoy's CLI mode hands off to a child process, so the exit code of the
    # process we start says nothing. cli_done.txt in the exe directory is the
    # documented completion marker: content 0 means success, 1 means failure.
    $workDir = Split-Path -Parent $exe.FullName
    foreach ($leftover in @('cli_done.txt', 'cli_percent.txt', 'cli_log.txt')) {
        $path = Join-Path $workDir $leftover
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }

    # Deliberately no /NOUSBCheck: Ventoy's own refusal to touch a non-USB disk
    # is a second, independent guard behind the checks above. exFAT is what the
    # bundle's sanitised package filenames were prepared for.
    $arguments = @('VTOYCLI', '/I', "/PhyDrive:$($target.Number)", '/FS:exFAT')
    Write-Info "$($exe.FullName) $($arguments -join ' ')"
    $process = Start-Process -FilePath $exe.FullName -ArgumentList $arguments `
                             -WorkingDirectory $workDir -Wait -PassThru -NoNewWindow

    $donePath = Join-Path $workDir 'cli_done.txt'
    $deadline = (Get-Date).AddMinutes(15)
    while (-not (Test-Path -LiteralPath $donePath) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Path -LiteralPath $donePath)) {
        throw "Ventoy did not finish within 15 minutes (process exit code $($process.ExitCode)). See $workDir\cli_log.txt."
    }
    $result = (Get-Content -LiteralPath $donePath -Raw).Trim()
    if ($result -ne '0') {
        $log = Join-Path $workDir 'cli_log.txt'
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 20 | ForEach-Object { Write-Info $_ } }
        throw "Ventoy installation failed (cli_done.txt = $result). Full log: $log"
    }

    Write-Host 'Ventoy installed.' -ForegroundColor Green

    # Windows needs a moment to surface the new volume.
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $found = @(Get-VentoyVolume)
        if ($found.Count -gt 0) { return $found[0] }
        Start-Sleep -Seconds 3
    }
    throw 'Ventoy reported success, but no volume labelled Ventoy appeared. Replug the stick and run prepare-usb.ps1 again.'
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

if ($Check -and $InstallVentoy) {
    throw '-Check and -InstallVentoy are mutually exclusive.'
}

$release = Get-Release
Write-Host "gpu-triage USB preparation" -ForegroundColor White
Write-Info "release.json: ISO $($release.iso_date), kernel $($release.expected_kernel), $($release.bundle_packages) packages"

if ($InstallVentoy) {
    $Drive = Install-VentoyOnDisk
}

$targetDrive = Resolve-TargetDrive
$driveRoot = "$targetDrive\"

if ($Check) {
    Invoke-Check -DriveLetter $targetDrive -Release $release

    $failed = @($script:Findings | Where-Object { $_.State -eq 'FAIL' })
    $warned = @($script:Findings | Where-Object { $_.State -eq 'WARN' })
    Write-Host ''
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) problem(s) - this stick will not boot into a working diagnosis." -ForegroundColor Red
        Write-Host 'Run prepare-usb.ps1 without -Check to repair it.' -ForegroundColor Red
        exit 1
    }
    if ($warned.Count -gt 0) {
        Write-Host "Ready to boot, with $($warned.Count) note(s)." -ForegroundColor Yellow
    } else {
        Write-Host 'Ready to boot.' -ForegroundColor Green
    }
    exit 0
}

# --- 1. what is already in place -----------------------------------------
Write-Step "Preparing $targetDrive - $(Get-DriveDescription $targetDrive)"

$state = Read-State $targetDrive
$isoOnStick = Join-Path $driveRoot $release.iso_name
$isoState = $null
if ($state -and ($state.PSObject.Properties.Name -contains 'iso')) { $isoState = $state.iso }

$isoReady = $false
if (Test-VerifiedByState -Path $isoOnStick -StateEntry $isoState -ExpectedSha256 $release.iso_sha256) {
    $isoReady = $true
    Write-Info "ISO already verified on this stick: $($release.iso_name)"
} elseif ((Test-Path -LiteralPath $isoOnStick) -and
          ((Get-Item -LiteralPath $isoOnStick).Length -eq $release.iso_size)) {
    Write-Info "ISO present, verifying $(Format-Size $release.iso_size) - this takes a moment"
    if ((Get-Sha256 $isoOnStick) -eq $release.iso_sha256) { $isoReady = $true }
    else { Write-Warning 'ISO on the stick has the wrong hash and will be replaced.' }
}

$bundleReason = '-Force was given'
$bundleReady = $false
if (-not $Force) {
    $bundleReady = Test-BundleComplete -OfflinePath $OfflineDir -Release $release -Reason ([ref]$bundleReason)
}
if ($bundleReady) {
    Write-Info "Bundle already unpacked in offline\: $bundleReason"
} else {
    Write-Info "Bundle has to be fetched: $bundleReason"
}

# --- 2. space, before the first byte --------------------------------------
Write-Step 'Checking free space'

$repoPayload = Get-PayloadSize -Root $RepoRoot -ExcludeDirs $SyncExcludedDirs
$bundleOnDisk = [long]$release.bundle_size
$margin = 200MB

$stickNeeded = $margin + $repoPayload + $bundleOnDisk
if (-not $isoReady) { $stickNeeded += [long]$release.iso_size }
Assert-FreeSpace -Path $driveRoot -Needed $stickNeeded -What 'stick'

if (-not $isoReady -or -not $bundleReady) {
    if (-not (Test-Path -LiteralPath $CacheDir)) { New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null }
    $cacheNeeded = $margin
    if (-not $isoReady) { $cacheNeeded += [long]$release.iso_size }
    if (-not $bundleReady) { $cacheNeeded += [long]$release.bundle_size }
    Assert-FreeSpace -Path $CacheDir -Needed $cacheNeeded -What 'download cache'
}
if (-not $bundleReady) {
    Assert-FreeSpace -Path $RepoRoot -Needed ($bundleOnDisk + $margin) -What 'repository (unpacked bundle)'
}

# --- 3. ISO ----------------------------------------------------------------
Write-Step "Arch ISO $($release.iso_name)"

if ($isoReady) {
    Add-Finding OK "ISO already on the stick and verified"
} else {
    $cachedIso = Get-CachedFile -Name $release.iso_name -Urls @($release.iso_urls) `
                                -Sha256 $release.iso_sha256 -Size ([long]$release.iso_size)
    Write-Info "Copying the ISO onto $targetDrive"
    Copy-Item -LiteralPath $cachedIso -Destination $isoOnStick -Force
    Write-Info 'Verifying the copy on the stick'
    if ((Get-Sha256 $isoOnStick) -ne $release.iso_sha256) {
        throw "The ISO copied onto $targetDrive does not match its hash. The stick may be faulty."
    }
    Add-Finding OK "ISO copied and verified: $($release.iso_name)"
}

# --- 4. bundle -------------------------------------------------------------
Write-Step "Offline bundle $($release.bundle_name)"

if ($bundleReady) {
    Add-Finding OK "Bundle already unpacked: $bundleReason"
} else {
    $cachedBundle = Get-CachedFile -Name $release.bundle_name -Urls @($release.bundle_url) `
                                   -Sha256 $release.bundle_sha256 -Size ([long]$release.bundle_size)
    Expand-Bundle -ZipPath $cachedBundle -Target $OfflineDir
    $bundleReason = ''
    if (-not (Test-BundleComplete -OfflinePath $OfflineDir -Release $release -Reason ([ref]$bundleReason))) {
        throw "The unpacked bundle does not match release.json: $bundleReason"
    }
    Add-Finding OK "Bundle unpacked and consistent: $bundleReason"
}

# --- 5. mirror the repository ---------------------------------------------
# Runs *after* the bundle was unpacked into offline\, never before: robocopy
# /MIR removes everything on the stick that is not in the source.
Write-Step 'Mirroring the repository onto the stick'
# No $LASTEXITCODE check here: robocopy reports 1 for "files were copied", so a
# nonzero code is the normal case. sync-to-usb.ps1 owns that distinction and
# throws on codes >= 8, which is what actually means failure.
& $SyncScript -Drive $targetDrive

# --- 6. BOOT.txt ----------------------------------------------------------
# Written before the verification, not after: the check looks for BOOT.txt, and
# a run that reports it as missing and then creates it reads like a failure.
$bootTxt = Write-BootTxt -DriveLetter $targetDrive -Release $release
Write-Info "Boot instructions written: $bootTxt"
$safeBootTxt = Write-SafeBootTxt -DriveLetter $targetDrive
Write-Info "Safe-boot instructions written: $safeBootTxt"

# --- 7. verify what actually arrived --------------------------------------
Write-Step 'Verifying the stick'
Invoke-Check -DriveLetter $targetDrive -Release $release -SkipIsoHash

Write-State -DriveLetter $targetDrive -State @{
    iso     = New-StateEntry -Path $isoOnStick -Sha256 $release.iso_sha256
    release = @{ iso_date = $release.iso_date; bundle_name = $release.bundle_name }
    written = (Get-Date).ToUniversalTime().ToString('o')
}

$failed = @($script:Findings | Where-Object { $_.State -eq 'FAIL' })
Write-Host ''
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) problem(s) remain - the stick is not ready." -ForegroundColor Red
    exit 1
}

Write-Host 'USB stick ready.' -ForegroundColor Green
Write-Host ''
Write-Host "Boot it, pick $($release.iso_name) in the Ventoy menu, then type:"
Write-Host '  d=$(readlink -f /dev/disk/by-label/Ventoy); m=/mnt/v; mkdir -p $m; mount /dev/mapper/${d##*/} $m || mount "$d" $m; bash $m/gpu-triage/go.sh list' -ForegroundColor White
Write-Host ''
Write-Info "The same line is in BOOT.txt on the stick root."
Write-Info "Download cache: $CacheDir (safe to delete, costs a re-download)"
