$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$logDir = Join-Path $PSScriptRoot 'run_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Windows PowerShell 5.1 may turn native stderr into a terminating error when
# ErrorActionPreference=Stop.  Missing modules then appear as only "Traceback".
# Run Python commands with Continue and retain their COMPLETE stdout/stderr.
$script:setupLog = Join-Path $logDir 'setup_diagnostics.log'
("Setup started: " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) | Set-Content -LiteralPath $script:setupLog -Encoding UTF8

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory=$true)][string]$File,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [string]$Step = 'command',
        [switch]$ShowOutput
    )
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $exitCode = 999
    try {
        Add-Content -LiteralPath $script:setupLog -Value ("`r`n=== " + $Step + " ===`r`n" + $File + ' ' + ($Arguments -join ' ')) -Encoding UTF8
        & $File @Arguments 2>&1 | ForEach-Object {
            $line = [string]$_
            [void]$lines.Add($line)
            Add-Content -LiteralPath $script:setupLog -Value $line -Encoding UTF8
            if ($ShowOutput) { Write-Host $line }
        }
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 999 }
    } catch {
        $line = 'Launcher invocation error: ' + $_.Exception.ToString()
        [void]$lines.Add($line)
        Add-Content -LiteralPath $script:setupLog -Value $line -Encoding UTF8
        if ($ShowOutput) { Write-Host $line -ForegroundColor Red }
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    Add-Content -LiteralPath $script:setupLog -Value ("Exit code: " + $exitCode) -Encoding UTF8
    return [pscustomobject]@{
        Code = [int]$exitCode
        Output = (($lines | Select-Object -Last 80) -join [Environment]::NewLine)
    }
}

function New-PrivateEnvironment {
    param([string]$BasePython, [string]$VenvDir, [string]$VenvPython)
    Write-Host 'Creating a fresh private Python environment...'
    if (Test-Path -LiteralPath $VenvDir) {
        try { Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction Stop }
        catch { throw ('Could not remove broken .runtime_venv. Close running Python processes and retry. ' + $_.Exception.Message) }
    }
    $result = Invoke-NativeCommand -File $BasePython -Arguments @('-m','venv',$VenvDir) -Step 'Create venv' -ShowOutput
    if ($result.Code -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw ('Python virtual environment creation failed. ' + $result.Output)
    }
}

function Test-WindowsPython([string]$exe) {
    if ([string]::IsNullOrWhiteSpace($exe) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $false }
    if ($exe -match '(?i)[\\/](msys64|mingw64|mingw32|cygwin64|WindowsApps)[\\/]') { return $false }
    try {
        # Important: Windows PowerShell 5.1 strips embedded double quotes when
        # passing -c to a native executable. Use a quote-free Python expression.
        # Earlier versions silently rejected an already installed Python.
        $probe = 'import sys,venv,ensurepip; print(int(sys.platform.startswith(chr(119)) and sys.version_info >= (3,10)))'
        $output = & $exe -c $probe 2>$null
        $ok = ($LASTEXITCODE -eq 0 -and [string]($output | Select-Object -Last 1) -eq '1')
        if ($ok) { return $true }
    } catch {}
    return $false
}

function Find-WindowsPython {
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    # Typical per-user / system installs, including Python installed to a custom directory.
    foreach ($v in @('312','313','314','311','310','315')) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"))
        if ($env:ProgramFiles) { $candidates.Add((Join-Path $env:ProgramFiles "Python$v\python.exe")) }
    }
    if ($env:LOCALAPPDATA) {
        $root = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $candidates.Add((Join-Path $_.FullName 'python.exe'))
            }
        }
    }
    # PEP 514 Python registry keys (both user and machine scopes).
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        if (-not (Test-Path $root)) { continue }
        Get-ChildItem -Path $root -ErrorAction SilentlyContinue | ForEach-Object {
            $installKey = Join-Path $_.PSPath 'InstallPath'
            if (Test-Path $installKey) {
                try {
                    $key = Get-Item -LiteralPath $installKey
                    $dir = $key.GetValue('')
                    $full = $key.GetValue('ExecutablePath')
                    if ($full) { $candidates.Add([string]$full) }
                    if ($dir) { $candidates.Add((Join-Path ([string]$dir) 'python.exe')) }
                } catch {}
            }
        }
    }
    # Python launcher, which may know about installs not on PATH.
    try {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            foreach ($version in @('-3.12','-3.13','-3.14','-3.11','-3.10','-3')) {
                $out = & $launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $out) {
                    $candidates.Add([string]($out | Select-Object -Last 1))
                }
            }
        }
    } catch {}
    try {
        $paths = & where.exe python 2>$null
        foreach ($p in $paths) { if ($p) { $candidates.Add([string]$p) } }
    } catch {}
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-WindowsPython $candidate) { return $candidate }
    }
    return $null
}

function Install-OfficialPython {
    $version = '3.12.10'
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $installer = Join-Path $env:TEMP "pta-python-$version-amd64.exe"
    $installLog = Join-Path $logDir 'python_install.log'
    Write-Host ''
    Write-Host 'No usable standard Windows Python was found.' -ForegroundColor Yellow
    Write-Host 'The launcher can install official Python for the current Windows user.'
    Write-Host "Download: $url"
    Write-Host 'No administrator rights or PyCharm required.'
    $answer = Read-Host 'Install Python now? Type Y to continue'
    if ($answer -notmatch '^(?i:y|yes)$') { throw 'Installation cancelled by user.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host 'Downloading official Python installer...'
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing -TimeoutSec 300
    if ((Get-Item -LiteralPath $installer).Length -lt 10000000) { throw 'Installer download is incomplete.' }
    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'Python installer signature verification failed.'
    }
    Write-Host 'Signature verified. Installing (per-user)...'
    # Do NOT force TargetDir: Python may already be installed in a non-default folder.
    # The original launcher assumed one exact path and could report a false failure.
    $arguments = '/quiet InstallAllUsers=0 Include_pip=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_test=0 PrependPath=0 /log "' + $installLog + '"'
    $proc = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    Write-Host "Python installer exit code: $($proc.ExitCode)"
    if ($proc.ExitCode -eq 3010) { Write-Host 'Installer requested a reboot. If detection fails, reboot and retry.' -ForegroundColor Yellow }
    elseif ($proc.ExitCode -ne 0) { throw "Python installer failed (exit $($proc.ExitCode)). See run_logs\python_install.log" }
    for ($i=0; $i -lt 10; $i++) {
        $found = Find-WindowsPython
        if ($found) { return $found }
        Start-Sleep -Seconds 2
    }
    Write-Host "Install log: $installLog" -ForegroundColor Yellow
    if (Test-Path $installLog) {
        Write-Host 'Last installer log lines:'
        Get-Content -LiteralPath $installLog -Tail 18 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    throw 'Installer finished but Windows Python is still not detected. Send run_logs\python_install.log for diagnosis.'
}

try {
    Write-Host '[1/4] Detecting Windows Python (excluding MSYS2)...'
    $basePython = Find-WindowsPython
    if (-not $basePython) {
        $expected = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        Write-Host "Checking Python at: $expected"
        if (Test-Path -LiteralPath $expected) {
            Write-Host 'Python executable exists but failed validation. Recording diagnostic...' -ForegroundColor Yellow
            $diag = Join-Path $logDir 'python_probe.txt'
            @(
                "Python executable: $expected",
                'Python --version:',
                (& $expected --version 2>&1 | Out-String),
                'Python interpreter probe:',
                (& $expected -c 'import sys,venv,ensurepip; print(sys.executable); print(sys.version); print(sys.platform)' 2>&1 | Out-String)
            ) | Out-File -LiteralPath $diag -Encoding UTF8
            Write-Host "Diagnostic: $diag"
        }
        $basePython = Install-OfficialPython
    }
    Write-Host "Using Windows Python: $basePython" -ForegroundColor Green

    $venvDir = Join-Path $PSScriptRoot '.runtime_venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (-not (Test-WindowsPython $venvPython)) {
        Write-Host '[2/4] Preparing private Python environment...'
        New-PrivateEnvironment -BasePython $basePython -VenvDir $venvDir -VenvPython $venvPython
    } else { Write-Host '[2/4] Reusing private Python environment.' }

    Write-Host '[3/4] Checking pip and Playwright...'
    $pipResult = Invoke-NativeCommand -File $venvPython -Arguments @('-m','pip','--version') -Step 'Check pip'
    if ($pipResult.Code -ne 0) {
        Write-Host 'pip is unavailable. Attempting ensurepip repair...' -ForegroundColor Yellow
        $repair = Invoke-NativeCommand -File $venvPython -Arguments @('-m','ensurepip','--upgrade') -Step 'Repair pip' -ShowOutput
        $pipResult = Invoke-NativeCommand -File $venvPython -Arguments @('-m','pip','--version') -Step 'Recheck pip'
        if ($pipResult.Code -ne 0) {
            Write-Host 'pip still fails. Recreating private environment once...' -ForegroundColor Yellow
            New-PrivateEnvironment -BasePython $basePython -VenvDir $venvDir -VenvPython $venvPython
            $pipResult = Invoke-NativeCommand -File $venvPython -Arguments @('-m','pip','--version') -Step 'Check pip after rebuild'
        }
    }
    if ($pipResult.Code -ne 0) {
        throw ('pip cannot run after repair. See run_logs\setup_diagnostics.log. ' + $pipResult.Output)
    }
    Write-Host ('pip OK: ' + ($pipResult.Output.Trim())) -ForegroundColor Green

    $pwResult = Invoke-NativeCommand -File $venvPython -Arguments @('-c','import playwright.sync_api') -Step 'Check Playwright'
    if ($pwResult.Code -ne 0) {
        Write-Host 'Playwright is missing or damaged. Installing (may take a few minutes)...' -ForegroundColor Yellow
        $install = Invoke-NativeCommand -File $venvPython -Arguments @('-m','pip','install','--disable-pip-version-check','--upgrade','--force-reinstall','playwright','-i','https://pypi.tuna.tsinghua.edu.cn/simple') -Step 'Install Playwright mirror' -ShowOutput
        if ($install.Code -ne 0) {
            Write-Host 'Mirror failed. Trying official PyPI...' -ForegroundColor Yellow
            $install = Invoke-NativeCommand -File $venvPython -Arguments @('-m','pip','install','--disable-pip-version-check','--upgrade','--force-reinstall','playwright','-i','https://pypi.org/simple') -Step 'Install Playwright PyPI' -ShowOutput
        }
        if ($install.Code -ne 0) {
            throw ('Playwright installation failed. Check network, and share run_logs\setup_diagnostics.log. ' + $install.Output)
        }
        $pwResult = Invoke-NativeCommand -File $venvPython -Arguments @('-c','import playwright.sync_api') -Step 'Verify Playwright installation'
        if ($pwResult.Code -ne 0) {
            throw ('Playwright still cannot import after installation. ' + $pwResult.Output)
        }
    }
    Write-Host 'Playwright OK.' -ForegroundColor Green

    Write-Host '[4/4] Checking browser...'
    $browserPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    )
    if ($env:ProgramFiles) {
        $browserPaths += (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe')
        $browserPaths += (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    }
    if (${env:ProgramFiles(x86)}) {
        $browserPaths += (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
        $browserPaths += (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
    }
    $hasBrowser = @($browserPaths | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0
    if (-not $hasBrowser) {
        Write-Host 'Chrome/Edge not found; downloading Playwright Chromium...'
        $chromium = Invoke-NativeCommand -File $venvPython -Arguments @('-m','playwright','install','chromium') -Step 'Install Chromium' -ShowOutput
        if ($chromium.Code -ne 0) { throw ('Chromium download failed. Install Chrome/Edge and retry. ' + $chromium.Output) }
    }
    Write-Host ''
    Write-Host 'Launching PTA Doubao automation...' -ForegroundColor Green
    # Run the automation with live output; keep its full output in a separate log.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $venvPython (Join-Path $PSScriptRoot 'pta_doubao.py') 2>&1 | Tee-Object -FilePath (Join-Path $logDir 'automation_console.log') -Append
        $runCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    exit $runCode
} catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Full setup log: run_logs\setup_diagnostics.log' -ForegroundColor Yellow
    if (Test-Path -LiteralPath $script:setupLog) {
        Write-Host 'Last 20 diagnostic lines:' -ForegroundColor Yellow
        Get-Content -LiteralPath $script:setupLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    Write-Host 'Please share run_logs\setup_diagnostics.log if setup still fails.'
    exit 2
}
