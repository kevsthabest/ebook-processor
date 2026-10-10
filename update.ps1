# update.ps1 - Pull the latest ebook-processor and report status.
# Usage: .\update.ps1
# Run this before your normal processing command to ensure you're on latest.

$ErrorActionPreference = "Stop"

try {
    git pull --ff-only origin master
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Updated to latest. Run your normal command now." -ForegroundColor Green
    } else {
        Write-Host "Pull failed (exit code $LASTEXITCODE). Check for local changes or conflicts." -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "Update failed: $_" -ForegroundColor Red
    exit 1
}
