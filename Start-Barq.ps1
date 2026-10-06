param(
    [ValidateRange(1024,262144)][int]$Context = 262144,
    [ValidateRange(1,65535)][int]$ApiPort = 8081,
    [ValidateRange(1,65535)][int]$BackendPort = 8082,
    [string]$PythonPath = ''
)
$ErrorActionPreference = 'Stop'
$BarqRoot = $PSScriptRoot
if ($ApiPort -eq $BackendPort) { throw 'API and backend ports must differ.' }
# Do not start a second model on top of an existing inference process.
$ExistingRuntime = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'llama-server.exe' })
if ($ExistingRuntime.Count -gt 0) {
    throw 'An inference server is already running. Close it yourself before starting Barq. No process was stopped.'
}
foreach ($Port in @($ApiPort,$BackendPort)) {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $Port is already in use. No existing service was stopped."
    }
}
if (-not $PythonPath) {
    $Candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    if (Test-Path -LiteralPath $Candidate) { $PythonPath = $Candidate }
    else {
        $Command = Get-Command python -ErrorAction SilentlyContinue
        if ($Command) { $PythonPath = $Command.Source }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Python 3.10+ is required for the local gateway. Supply -PythonPath; no packages need installation.'
}
$LogDir = Join-Path $BarqRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Runtime = Join-Path $BarqRoot 'runtime\llama-server.exe'
$Package = Get-Content -LiteralPath (Join-Path $BarqRoot 'config\Barq-Package.json') -Raw | ConvertFrom-Json
if ([System.IO.Path]::IsPathRooted($Package.active_model)) { throw 'Package model must be a relative path.' }
$ModelsRoot = [System.IO.Path]::GetFullPath((Join-Path $BarqRoot 'models')) + [System.IO.Path]::DirectorySeparatorChar
$Model = [System.IO.Path]::GetFullPath((Join-Path $BarqRoot $Package.active_model))
if (-not $Model.StartsWith($ModelsRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Package model must stay inside the models directory.' }
$Projector = Join-Path $BarqRoot 'models\Barq-27B-mmproj-Q8_0.gguf'
$Gateway = Join-Path $BarqRoot 'server\Barq-Gateway.py'
foreach ($File in @($Runtime,$Model,$Projector,$Gateway)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Required file missing: $File" }
}
$Manifest = Get-Content -LiteralPath (Join-Path $BarqRoot 'legal\Installation-Manifest.json') -Raw | ConvertFrom-Json
$PolicyRecord = @($Manifest.package_files | Where-Object { $_.path.Replace('\','/') -eq 'prompts/Barq-27B-System.txt' })
if ($PolicyRecord.Count -ne 1) { throw 'Central policy manifest is missing or ambiguous.' }
$PolicyHash = (Get-FileHash -LiteralPath (Join-Path $BarqRoot 'prompts\Barq-27B-System.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
if ($PolicyHash -ne $PolicyRecord[0].sha256 -or $PolicyHash -ne $Package.policy_sha256) {
    throw 'Central policy changed. Rebuild the GGUF policy and manifest together before launching.'
}
$ActiveRecord = $Manifest.active_policy
if ($null -eq $ActiveRecord -or $ActiveRecord.policy_sha256 -ne $PolicyHash -or $ActiveRecord.model -ne $Package.active_model) {
    throw 'Active model and policy do not match the installation manifest.'
}
if ((Get-Item -LiteralPath $Model).Length -ne $ActiveRecord.model_bytes) { throw 'Active model size differs from its installation record.' }
# Mirror saved logs to this terminal without printing request bodies.
function Write-BarqLogLines {
    param([System.IO.StreamReader]$Reader, [string]$Label)
    for ($LineNumber = 0; $LineNumber -lt 200; $LineNumber++) {
        if ($Reader.EndOfStream) { break }
        $Line = $Reader.ReadLine()
        if ($null -ne $Line) { Write-Host "[$Label] $Line" }
    }
}
$Backend = $null
$GatewayProcess = $null
$LogReaders = @()
try {
    # Explicit quoting is required because the package directory contains a space.
    $RuntimeArgs = @(
        '-m', ('"' + $Model + '"'), '--host', '127.0.0.1', '--port', "$BackendPort",
        '--alias', 'Barq-27B', '-ngl', '99', '-fa', 'on', '-c', "$Context", '-np', '1',
        '--temp', '1.0', '--top-p', '0.95', '--top-k', '20', '--min-p', '0.05',
        '--jinja', '--reasoning-effort', 'medium', '--reasoning-budget', '-1',
        '--cache-type-k', 'q4_0', '--cache-type-v', 'q4_0',
        '--mmproj', ('"' + $Projector + '"'), '--no-mmproj-offload'
    )
    Write-Host 'Barq 27B - launch configuration' -ForegroundColor Cyan
    Write-Host "Model: $Model"
    Write-Host "Policy: $($Package.policy_version) | Memory: scoped gateway commands; MCP optional"
    Write-Host "Runtime: $Runtime"
    Write-Host "Context: $Context | Slots: 1 | KV cache: Q4/Q4"
    Write-Host 'GPU offload requested: 99 layers | Flash attention: on | Vision projector: CPU'
    Write-Host 'Reasoning modes: Default, Low, Medium, High, Xhigh, Max (selected by the client)'
    Write-Host "Client API: http://127.0.0.1:$ApiPort/v1"
    Write-Host "Internal backend: http://127.0.0.1:$BackendPort"
    Write-Host "Logs: $LogDir"
    Write-Host 'Loading starts below. Readiness is confirmed by the runtime model-loaded/listening messages.'
    Write-Host 'Leave this terminal open. Ctrl+C ends this launch.'
    $RuntimeOut = Join-Path $LogDir 'Barq-Runtime.out.log'
    $RuntimeErr = Join-Path $LogDir 'Barq-Runtime.err.log'
    $GatewayOut = Join-Path $LogDir 'Barq-Gateway.out.log'
    $GatewayErr = Join-Path $LogDir 'Barq-Gateway.err.log'
    $Backend = Start-Process -FilePath $Runtime -ArgumentList $RuntimeArgs -WorkingDirectory (Split-Path $Runtime) `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $RuntimeOut -RedirectStandardError $RuntimeErr
    $GatewayArgs = @('-u', ('"' + $Gateway + '"'), '--port', "$ApiPort", '--backend-port', "$BackendPort", '--context', "$Context")
    $GatewayProcess = Start-Process -FilePath $PythonPath -ArgumentList $GatewayArgs -WorkingDirectory $BarqRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $GatewayOut -RedirectStandardError $GatewayErr
    foreach ($Log in @(
        @{ Path = $RuntimeOut; Label = 'runtime' },
        @{ Path = $RuntimeErr; Label = 'runtime' },
        @{ Path = $GatewayOut; Label = 'api' },
        @{ Path = $GatewayErr; Label = 'api' }
    )) {
        $Stream = [System.IO.File]::Open($Log.Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, `
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        $Reader = [System.IO.StreamReader]::new($Stream, [System.Text.Encoding]::UTF8, $true)
        $LogReaders += @{ Reader = $Reader; Label = $Log.Label }
    }
    while ($true) {
        foreach ($Log in $LogReaders) { Write-BarqLogLines -Reader $Log.Reader -Label $Log.Label }
        $Backend.Refresh()
        $GatewayProcess.Refresh()
        if ($Backend.HasExited -or $GatewayProcess.HasExited) {
            foreach ($Log in $LogReaders) { Write-BarqLogLines -Reader $Log.Reader -Label $Log.Label }
            if ($Backend.HasExited) { throw "Model runtime exited with code $($Backend.ExitCode). See the runtime logs above." }
            if ($GatewayProcess.ExitCode -ne 0) { throw "Gateway exited with code $($GatewayProcess.ExitCode). See the API logs above." }
            break
        }
        Start-Sleep -Milliseconds 200
    }
} finally {
    foreach ($Log in $LogReaders) { $Log.Reader.Dispose() }
    # Only processes created by this invocation are owned by this launcher.
    foreach ($OwnedProcess in @($GatewayProcess, $Backend)) {
        if ($OwnedProcess) {
            $OwnedProcess.Refresh()
            if (-not $OwnedProcess.HasExited) { $OwnedProcess.Kill(); $OwnedProcess.WaitForExit() }
            $OwnedProcess.Dispose()
        }
    }
}
