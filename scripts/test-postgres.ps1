[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$PostgresBin,

    [ValidateRange(1, 65535)]
    [int]$Port = 55432
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Port -eq 5432) {
    throw "Port 5432 is reserved for the existing PostgreSQL service; choose a disposable test port."
}

function Test-DisposableClusterPath {
    param([Parameter(Mandatory)][string]$Candidate)

    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $trimmedRoot = $tempRoot.TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
    $rootPrefix = $trimmedRoot + [IO.Path]::DirectorySeparatorChar
    $fullCandidate = [IO.Path]::GetFullPath($Candidate)

    if (-not $fullCandidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }

    $childName = [IO.Path]::GetFileName($fullCandidate)
    return $childName -match '^tenable-ingestion-pg-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
}

function Invoke-Postgres {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

$postgresBinPath = [IO.Path]::GetFullPath($PostgresBin)
foreach ($name in @("initdb.exe", "pg_ctl.exe", "createdb.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $postgresBinPath $name) -PathType Leaf)) {
        throw "PostgreSQL executable not found: $(Join-Path $postgresBinPath $name)"
    }
}

$clusterPath = [IO.Path]::GetFullPath(
    (Join-Path ([IO.Path]::GetTempPath()) ("tenable-ingestion-pg-{0}" -f [guid]::NewGuid()))
)
if (-not (Test-DisposableClusterPath $clusterPath)) {
    throw "Refusing unsafe temporary PostgreSQL directory: $clusterPath"
}

$initdb = Join-Path $postgresBinPath "initdb.exe"
$pgCtl = Join-Path $postgresBinPath "pg_ctl.exe"
$createdb = Join-Path $postgresBinPath "createdb.exe"
$venvPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    [IO.Path]::GetFullPath($venvPython)
} else {
    (Get-Command python -ErrorAction Stop).Source
}

$serverStartAttempted = $false
$postgresPid = $null

try {
    Write-Host "Creating disposable PostgreSQL cluster: $clusterPath"
    Invoke-Postgres $initdb @("--auth=trust", "--encoding=UTF8", "--username=postgres", "--pgdata=$clusterPath")

    $serverStartAttempted = $true
    Invoke-Postgres $pgCtl @(
        "-D", $clusterPath,
        "-l", (Join-Path $clusterPath "postgresql.log"),
        "-o", "-h 127.0.0.1 -p $Port",
        "-w", "start"
    )
    $postgresPid = [int](Get-Content -LiteralPath (Join-Path $clusterPath "postmaster.pid") -TotalCount 1)
    Write-Host "Temporary PostgreSQL PID: $postgresPid"

    Invoke-Postgres $createdb @("-h", "127.0.0.1", "-p", "$Port", "-U", "postgres", "tenable_ingestion_test")
    $env:TEST_PG_DSN = "postgresql://postgres@127.0.0.1:$Port/tenable_ingestion_test"

    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "pytest failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($serverStartAttempted) {
        $pidFile = Join-Path $clusterPath "postmaster.pid"
        if ($null -eq $postgresPid -and (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
            $postgresPid = [int](Get-Content -LiteralPath $pidFile -TotalCount 1)
        }

        & $pgCtl -D $clusterPath status
        $statusExitCode = $LASTEXITCODE
        & $pgCtl -D $clusterPath -m fast -w stop
        $stopExitCode = $LASTEXITCODE

        if ($null -ne $postgresPid -and (Get-Process -Id $postgresPid -ErrorAction SilentlyContinue)) {
            throw "Temporary PostgreSQL PID $postgresPid is still running after pg_ctl stop; refusing cleanup."
        }
        if ($statusExitCode -eq 0 -and $stopExitCode -ne 0) {
            throw "pg_ctl reported a running temporary server but stop failed ($stopExitCode); refusing cleanup."
        }
    }

    if (-not (Test-DisposableClusterPath $clusterPath)) {
        throw "Refusing unsafe temporary PostgreSQL cleanup: $clusterPath"
    }
    if (Test-Path -LiteralPath $clusterPath) {
        Remove-Item -LiteralPath $clusterPath -Recurse -Force
    }
    Write-Host "Cleanup confirmed: temporary PostgreSQL directory removed."
}
