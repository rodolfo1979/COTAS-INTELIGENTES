$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\rfall\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Port = if ($args.Count -gt 0) { [int]$args[0] } else { 8088 }
$OutLog = Join-Path $Root "storage\web_app.out.log"
$ErrLog = Join-Path $Root "storage\web_app.err.log"

New-Item -ItemType Directory -Force -Path (Join-Path $Root "storage") | Out-Null

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Planos Cotas ya esta corriendo en http://127.0.0.1:$Port"
    exit 0
}

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $Python
$psi.WorkingDirectory = $Root
$psi.Arguments = "`"tools\web_app.py`" $Port"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

# Some shells expose both Path and PATH. Windows process creation treats them as
# duplicates, so normalize the child environment before starting Python.
$pathValue = $psi.EnvironmentVariables["Path"]
if (-not $pathValue) {
    $pathValue = $psi.EnvironmentVariables["PATH"]
}
$psi.EnvironmentVariables.Remove("PATH")
if ($pathValue) {
    $psi.EnvironmentVariables["Path"] = $pathValue
}

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $psi
$null = $process.Start()

Start-Sleep -Milliseconds 800
if ($process.HasExited) {
    $stderr = $process.StandardError.ReadToEnd()
    $stdout = $process.StandardOutput.ReadToEnd()
    Set-Content -LiteralPath $ErrLog -Value $stderr -Encoding UTF8
    Set-Content -LiteralPath $OutLog -Value $stdout -Encoding UTF8
    throw "No se pudo iniciar el servidor. Revise $ErrLog"
}

Write-Output "Planos Cotas listo en http://127.0.0.1:$Port"
