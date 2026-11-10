# Run the console on Windows. The run.sh counterpart: safe to re-run -- use
# the venv if it exists, otherwise create it first. `.\run.ps1 --check` only
# checks the environment. The browser runs headed by default (a Windows
# desktop always has a screen, no Xvfb needed) and secrets fall back to the
# file backend in %LOCALAPPDATA%\GaiConsole (Windows has no built-in keyring
# CLI; protection is the user profile ACL).
#
# This file is deliberately pure ASCII: Windows PowerShell 5.1 reads a .ps1
# without a BOM as ANSI (cp1252), and characters such as an em-dash inside a
# string can break the parser before the first line runs.
$ErrorActionPreference = "Stop"

# Force UTF-8 for child Python processes: without this, stdout on Windows
# uses the local codepage (cp1252) and console/flow logs full of non-ASCII
# characters can die with UnicodeEncodeError.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Push-Location $PSScriptRoot
try {
    # Saved to restore in finally: the caller's session GAI_BIND_PORT must
    # not change permanently just because this script ran.
    $PrevBindPort = $env:GAI_BIND_PORT
    function Say($msg) { Write-Host $msg }
    function Die($msg) { Write-Host "failed: $msg"; exit 1 }

    # -- Python 3.12+ -------------------------------------------------------
    # pyproject requires >=3.12. The `py` launcher is the standard path for
    # python.org installs; `python` on PATH is the fallback.
    function Find-Python {
        foreach ($cand in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3"), @("python", ""))) {
            $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
            if (-not $exe) { continue }
            $extra = @($cand[1]) | Where-Object { $_ }
            try {
                $ver = & $exe.Source @extra -c "import sys; print(sys.version_info[:2] >= (3, 12))" 2>$null
            } catch { continue }
            if ($ver -eq "True") { return @($exe.Source) + $extra }
        }
        return $null
    }

    # @(...) keeps a 1-element result as an array; without it PowerShell
    # unrolls it into a string and $Py[0] takes its first character.
    $Py = @(Find-Python)
    if ($Py.Count -eq 0) { Die "Python 3.12+ not found. Install it first from https://www.python.org/downloads/ (tick 'py launcher')" }
    $PyExe = $Py[0]
    $PyArgs = @($Py | Select-Object -Skip 1)

    $VenvPy = ".venv\Scripts\python.exe"
    if (-not (Test-Path $VenvPy)) {
        Say "creating .venv with $(& $PyExe @PyArgs -V 2>&1)"
        & $PyExe @PyArgs -m venv .venv
        if ($LASTEXITCODE -ne 0) { Die "venv failed" }
    }

    $depsOk = $false
    try {
        & $VenvPy -c "import fastapi, playwright, tzdata" 2>$null
        $depsOk = ($LASTEXITCODE -eq 0)
    } catch { $depsOk = $false }
    if (-not $depsOk) {
        Say "installing hash-pinned dependencies from requirements.lock"
        & $VenvPy -m pip install --quiet --upgrade pip
        & $VenvPy -m pip install --require-hashes --no-input -r requirements.lock
        if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
    }

    # -- browser ------------------------------------------------------------
    function Test-BrowserPresent {
        $paths = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Chromium\Application\chrome.exe",
            "$env:ProgramFiles\Chromium\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Chromium\Application\chrome.exe"
        )
        foreach ($p in $paths) { if (Test-Path $p) { return $true } }
        return [bool](Get-Command chrome -ErrorAction SilentlyContinue)
    }

    if (-not (Test-BrowserPresent)) {
        if ($env:GAI_SKIP_BROWSER_INSTALL -eq "1") {
            Say "warning: no Chrome/Chromium, and GAI_SKIP_BROWSER_INSTALL=1 -- launch will fail"
        } else {
            Say "no system Chrome/Chromium -- installing Playwright's bundled chromium"
            & $VenvPy -m playwright install chromium
            if ($LASTEXITCODE -ne 0) { Say "warning: browser download failed; run manually: .venv\Scripts\python -m playwright install chromium" }
        }
    }

    # -- environment --------------------------------------------------------
    # PORT is the short name; GAI_BIND_PORT wins when both are set.
    # Computed into a local variable so the caller's PowerShell session env stays clean.
    $BindPort = if ($env:GAI_BIND_PORT) { $env:GAI_BIND_PORT } elseif ($env:PORT) { $env:PORT } else { "8765" }
    $BindHost = if ($env:GAI_BIND_HOST) { $env:GAI_BIND_HOST } else { "127.0.0.1" }

    if ($args.Count -gt 0 -and $args[0] -eq "--check") {
        Say "python   : $(& $VenvPy -V 2>&1)"
        Say "data dir : $(& $VenvPy -c 'from app.config import settings; print(settings.data_dir)')"
        Say "secret   : $(& $VenvPy -c 'from app.secrets import describe; print(describe())')"
        Say "browser  : $(& $VenvPy -c 'from app.runner.browser import describe; print(describe())')"
        Say "token    : $(& $VenvPy -c 'from app.config import settings; print(settings.data_dir / ''console.token'')')"
        exit 0
    }

    Say "note: no keyring CLI on Windows -- secrets are stored in vault.json in the data dir (user profile ACL)"

    # The app boot banner reads the port from env GAI_BIND_PORT; the child
    # process must see the same value as --port below, without polluting the
    # caller's session (restored in finally).
    $env:GAI_BIND_PORT = $BindPort
    & $VenvPy -m uvicorn app.main:app --host $BindHost --port $BindPort --log-level warning
    exit $LASTEXITCODE
}
finally {
    if ($null -eq $PrevBindPort) {
        Remove-Item Env:GAI_BIND_PORT -ErrorAction SilentlyContinue
    } else {
        $env:GAI_BIND_PORT = $PrevBindPort
    }
    Pop-Location
}
