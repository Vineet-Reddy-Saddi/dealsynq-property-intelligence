param(
    [int]$PrecomputeProcessId = 8700
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'pilots\stamford_ct\reports\stamford_ct_overnight_validation.log'

try {
    $precompute = Get-Process -Id $PrecomputeProcessId -ErrorAction SilentlyContinue
    if ($precompute) {
        Wait-Process -Id $PrecomputeProcessId
    }

    & 'C:\Python314\python.exe' (Join-Path $root 'scripts\validate_stamford_samples.py') *>> $log
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Push-Location $root
    try {
        & 'C:\Python314\python.exe' 'demo\scripts\export_demo_data.py' *>> $log
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Push-Location 'demo'
        try {
            npm run build *>> $log
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        finally { Pop-Location }
    }
    finally { Pop-Location }
}
catch {
    $_ | Out-String | Add-Content -LiteralPath $log
    exit 1
}
