<#
.SYNOPSIS
Run all PR 07 checks, including isolated browser and installed-package scenarios.
.DESCRIPTION
Uses the repository's .venv by default. Requires the development dependencies.
Each run gets a new artifacts/pr07-tests directory with logs, JUnit and JSON
results. Stops at the first failed check and exits 1; success exits 0.
Tests own their temporary projects, application servers and operator sessions.
No provider keys or manual browser interaction are needed. Live Windows UIA
tests require a separate interactive desktop gate and are excluded here.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci/test_pr07.ps1
.EXAMPLE
pwsh -File scripts/ci/test_pr07.ps1 -SkipInstalled
.EXAMPLE
pwsh -File scripts/ci/test_pr07.ps1 -AllBrowser -PythonPath C:/env/Scripts/python.exe
#>
[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$SkipInstalled,
    [switch]$AllBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $PythonPath) {
    $PythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    Write-Host "Python was not found at $PythonPath" -ForegroundColor Red
    Write-Host 'Use -PythonPath to select an environment with inter-cua[dev] installed.'
    exit 64
}
$script:PythonExecutable = (Resolve-Path -LiteralPath $PythonPath).Path

$runName = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$script:ReportRoot = Join-Path $repoRoot ('artifacts/pr07-tests/' + $runName)
$null = New-Item -ItemType Directory -Path $script:ReportRoot
$script:GateResults = [System.Collections.Generic.List[object]]::new()
$script:GateNumber = 0
$startedUtc = [datetime]::UtcNow.ToString('o')
$runExitCode = 1
$failureText = $null
$locationPushed = $false
$previousUtf8 = [System.Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
$previousUnbuffered = [System.Environment]::GetEnvironmentVariable('PYTHONUNBUFFERED', 'Process')

function Invoke-PythonGate {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $script:GateNumber++
    $logPath = Join-Path $script:ReportRoot ('{0:D2}-{1}.log' -f $script:GateNumber, $Name)
    Write-Host "`n[$($script:GateNumber)] $Name" -ForegroundColor Cyan
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell wraps redirected native stderr in ErrorRecord.
        # Warnings on stderr do not fail a gate; the process exit code decides.
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = $null
        & $script:PythonExecutable @Arguments 2>&1 |
            ForEach-Object { $_.ToString() } |
            Tee-Object -FilePath $logPath -ErrorAction Stop
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
        $timer.Stop()
    }
    if ($null -eq $nativeExitCode) {
        $nativeExitCode = -1
    }
    $script:GateResults.Add([pscustomobject]@{
        name = $Name
        passed = ($nativeExitCode -eq 0)
        exit_code = $nativeExitCode
        seconds = [math]::Round($timer.Elapsed.TotalSeconds, 2)
        log = $logPath
    })
    if ($nativeExitCode -ne 0) {
        throw "$Name failed (exit $nativeExitCode). See $logPath"
    }
}

try {
    Push-Location -LiteralPath $repoRoot
    $locationPushed = $true
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    Write-Host "Python: $script:PythonExecutable"
    Write-Host "Results: $script:ReportRoot"

    $modules = "'pytest', 'ruff', 'mypy', 'playwright'"
    if (-not $SkipInstalled) {
        $modules += ", 'build', 'hatchling'"
    }
    $dependencyCheck = @"
import importlib.util, sys
missing = [m for m in ($modules) if importlib.util.find_spec(m) is None]
if missing:
    print('Missing tools: ' + ', '.join(missing))
    print('Install development dependencies: python -m pip install -e ".[dev]"')
    sys.exit(1)
import cua
print('Development tools ready; inter-cua ' + cua.__version__)
"@
    Invoke-PythonGate 'environment' @('-c', $dependencyCheck)
    Invoke-PythonGate 'lint' @('-m', 'ruff', 'check', '.')
    Invoke-PythonGate 'format' @('-m', 'ruff', 'format', '--check', '.')
    Invoke-PythonGate 'types-linux' @('-m', 'mypy', '--platform', 'linux')
    Invoke-PythonGate 'types-windows' @('-m', 'mypy', '--platform', 'win32')
    Invoke-PythonGate 'unit-tests' @(
        '-m', 'pytest', '-m', 'not browser and not desktop',
        ('--junitxml=' + (Join-Path $script:ReportRoot 'unit.xml'))
    )
    Invoke-PythonGate 'chromium' @('-m', 'playwright', 'install', 'chromium')
    $browserArguments = @('-m', 'pytest', '--require-browser')
    if ($AllBrowser) {
        $browserArguments += @('-m', 'browser and not desktop')
    }
    else {
        $browserArguments += 'tests/integration/test_inventory.py'
    }
    $browserArguments += '--junitxml=' + (Join-Path $script:ReportRoot 'browser.xml')
    Invoke-PythonGate 'browser-tests' $browserArguments

    if (-not $SkipInstalled) {
        $distDir = Join-Path $script:ReportRoot 'dist'
        Invoke-PythonGate 'build' @('-m', 'build', '--no-isolation', '--outdir', $distDir)
        $wheels = @(Get-ChildItem -LiteralPath $distDir -Filter '*.whl' -File)
        $sdists = @(Get-ChildItem -LiteralPath $distDir -Filter '*.tar.gz' -File)
        if ($wheels.Count -ne 1 -or $sdists.Count -ne 1) {
            throw "Expected exactly one wheel and one source archive in $distDir"
        }
        Invoke-PythonGate 'archive-check' @(
            'scripts/ci/check_distribution.py', $wheels[0].FullName, $sdists[0].FullName
        )
        Invoke-PythonGate 'installed-wheel' @('scripts/ci/verify_installed.py', $distDir, '--browser')
        Invoke-PythonGate 'installed-sdist' @(
            'scripts/ci/verify_installed.py', $distDir, '--from-sdist', '--browser'
        )
    }
    $runExitCode = 0
    Write-Host "`nPASS: all selected PR 07 checks passed." -ForegroundColor Green
}
catch {
    $failureText = $_.Exception.Message
    Write-Host "`nFAIL: $failureText" -ForegroundColor Red
}
finally {
    [System.Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousUtf8, 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONUNBUFFERED', $previousUnbuffered, 'Process')
    if ($locationPushed) {
        Pop-Location
    }
    $summaryPath = Join-Path $script:ReportRoot 'summary.json'
    $summary = [ordered]@{
        phase = 'PR 07'
        passed = ($runExitCode -eq 0)
        exit_code = $runExitCode
        started_utc = $startedUtc
        finished_utc = [datetime]::UtcNow.ToString('o')
        python = $script:PythonExecutable
        all_browser = [bool]$AllBrowser
        installed_checks = (-not [bool]$SkipInstalled)
        gates = @($script:GateResults.ToArray())
        failure = $failureText
    } | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($summaryPath, $summary, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Summary: $summaryPath"
}
exit $runExitCode
