# Cross-OS launcher for gois on Windows (no admin required).
# Mirrors scripts/start.sh: ensures venv, installs dev deps, runs the monitor.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-SkipVendor]

[CmdletBinding()]
param(
    [switch]$SkipVendor,
    [switch]$Launchd  # accepted for parity with start.sh; ignored on Windows
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectDir

function Write-Info($msg) { Write-Host "[start] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[start] $msg" -ForegroundColor Yellow }
function Write-Err ($msg) { Write-Host "[start] $msg" -ForegroundColor Red }

# Locate a Python 3.11+ interpreter without requiring admin.
function Resolve-Python {
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd) {
            try {
                $args = if ($name -eq "py") { @("-3", "-c", "import sys;print(sys.version_info[:2])") } else { @("-c", "import sys;print(sys.version_info[:2])") }
                $version = & $cmd.Source @args 2>$null
                if ($LASTEXITCODE -eq 0 -and $version -match "\((\d+),\s*(\d+)\)") {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 11)) {
                        return @{ Cmd = $cmd.Source; UsePy = ($name -eq "py") }
                    }
                }
            } catch { }
        }
    }
    Write-Err "Python 3.11+ não encontrado. Instale via https://www.python.org/downloads/ (sem admin: 'Install for me only')."
    exit 2
}

$py = Resolve-Python
$pyArgs = if ($py.UsePy) { @("-3") } else { @() }

$venvDir = Join-Path $ProjectDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Info "Criando venv em .venv"
    & $py.Cmd @pyArgs "-m" "venv" $venvDir
    if ($LASTEXITCODE -ne 0) { Write-Err "falha ao criar venv"; exit 2 }
}

Write-Info "Instalando gois (editable)"
& $venvPython -m pip install --upgrade pip setuptools wheel --quiet --disable-pip-version-check
& $venvPython -m pip install -e ".[dev]" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Err "pip install falhou"; exit 2 }

# Bootstrap config/.env se não existirem (sem segredos)
$cfg = Join-Path $ProjectDir "config.yaml"
$cfgExample = Join-Path $ProjectDir "config.example.yaml"
if (-not (Test-Path $cfg) -and (Test-Path $cfgExample)) {
    Copy-Item -LiteralPath $cfgExample -Destination $cfg
    Write-Info "config.yaml criado a partir de config.example.yaml"
}
$envFile = Join-Path $ProjectDir ".env"
if (-not (Test-Path $envFile)) {
    "# Defina suas chaves LLM aqui (DEEPSEEK_API_KEY, OPENAI_API_KEY, etc.)`n" | Out-File -Encoding utf8 -FilePath $envFile
    Write-Info ".env criado vazio"
}

if (-not $SkipVendor) {
    Write-Warn "scripts/unify-stack.sh é Bash-only — pulando submódulos Hermes/OpenClaw no Windows."
    Write-Warn "Use WSL2 para a stack completa, ou rode --skip-vendor explicitamente."
}

Write-Info "Iniciando monitor: $venvPython -m gois --config $cfg"
& $venvPython "-m" "gois" "--config" $cfg
exit $LASTEXITCODE
