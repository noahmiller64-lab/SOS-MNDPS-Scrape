param(
    [string]$Repository = $PSScriptRoot,
    [string]$OutputsDirectory = (Join-Path $PSScriptRoot "outputs"),
    [string]$Python = "python",
    [switch]$PushCheckpoints
)

$ErrorActionPreference = "Stop"
$totalNames = 3308
$checkpoint = Join-Path $Repository "MNDPS_SOS_Search_Checkpoint.jsonl"
$logFile = Join-Path $Repository "mndps_sos_scrape_background.log"
$statusFile = Join-Path $Repository "mndps_sos_scrape_status.json"
$textOutput = Join-Path $Repository "MNDPS_SOS_Search_Results_AllTabs.txt"
$pdfOutput = Join-Path $Repository "MNDPS_SOS_Search_Results_AllTabs.pdf"

function Get-CheckpointCount {
    if (-not (Test-Path -LiteralPath $checkpoint)) {
        return 0
    }
    return (Get-Content -LiteralPath $checkpoint | Measure-Object).Count
}

function Write-RunStatus {
    param([string]$State, [int]$Completed, [string]$Message)
    [ordered]@{
        state = $State
        completed = $Completed
        total = $totalNames
        message = $Message
        updated_at = (Get-Date).ToString("o")
        log = $logFile
    } | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding utf8
}

Set-Location -LiteralPath $Repository
Write-RunStatus -State "scraping" -Completed (Get-CheckpointCount) -Message "Resuming the checkpointed MNDPS SOS scrape."

$lastFailedCount = -1
$samePointFailures = 0
$runAttempt = 0

while ($true) {
    $runAttempt += 1
    "[$((Get-Date).ToString('o'))] Starting/resuming scraper attempt $runAttempt." |
        Tee-Object -FilePath $logFile -Append

    $scrapeArgs = @(
        ".\scrape_mndps_sos.py",
        "--input", ".\sos_names_mndps_all_fy_tabs.txt",
        "--checkpoint", ".\MNDPS_SOS_Search_Checkpoint.jsonl",
        "--limit", $totalNames,
        "--delay", "1.05",
        "--checkpoint-every", "100"
    )
    if ($PushCheckpoints) {
        $scrapeArgs += "--push-checkpoints"
    }
    & $Python @scrapeArgs 2>&1 | Tee-Object -FilePath $logFile -Append
    $scrapeExit = $LASTEXITCODE
    $completed = Get-CheckpointCount
    if ($scrapeExit -eq 0) {
        break
    }

    if ($completed -eq $lastFailedCount) {
        $samePointFailures += 1
    } else {
        $samePointFailures = 1
        $lastFailedCount = $completed
    }
    Write-RunStatus -State "retrying" -Completed $completed -Message "Scraper exited with code $scrapeExit; retry $samePointFailures of 3 at this checkpoint."
    if ($samePointFailures -ge 3) {
        Write-RunStatus -State "blocked" -Completed $completed -Message "Scraper failed three times at the same checkpoint."
        exit $scrapeExit
    }
    Start-Sleep -Seconds (60 * $samePointFailures)
}

$completed = Get-CheckpointCount
Write-RunStatus -State "building" -Completed $completed -Message "Scrape complete; building final text and PDF deliverables."

$buildArgs = @(
    ".\build_mndps_sos_report.py",
    "--input", ".\sos_names_mndps_all_fy_tabs.txt",
    "--checkpoint", ".\MNDPS_SOS_Search_Checkpoint.jsonl",
    "--text-output", ".\MNDPS_SOS_Search_Results_AllTabs.txt",
    "--pdf-output", ".\MNDPS_SOS_Search_Results_AllTabs.pdf"
)
if ($PushCheckpoints) {
    $buildArgs += "--push"
}
& $Python @buildArgs 2>&1 | Tee-Object -FilePath $logFile -Append
$buildExit = $LASTEXITCODE
if ($buildExit -ne 0) {
    Write-RunStatus -State "blocked" -Completed $completed -Message "Final report build failed with code $buildExit."
    exit $buildExit
}

New-Item -ItemType Directory -Path $OutputsDirectory -Force | Out-Null
Copy-Item -LiteralPath $textOutput -Destination (Join-Path $OutputsDirectory (Split-Path $textOutput -Leaf)) -Force
Copy-Item -LiteralPath $pdfOutput -Destination (Join-Path $OutputsDirectory (Split-Path $pdfOutput -Leaf)) -Force

Write-RunStatus -State "complete" -Completed $totalNames -Message "Final text and PDF were built and copied to outputs."
"[$((Get-Date).ToString('o'))] Full MNDPS scrape and report build complete." |
    Tee-Object -FilePath $logFile -Append
