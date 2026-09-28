# Automated SDR by Fred - one-command installer for Windows (PowerShell 5.1 or newer).
#
#   irm https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.ps1 | iex
#
# What it does (no admin rights needed; if something is missing it tells you the command to run):
#   1. checks for git and Python 3.11 or newer
#   2. downloads the newest release into $env:SDR_HOME (default %USERPROFILE%\automated-sdr), or
#      moves an existing copy to it (never to unreleased code)
#   3. creates a private Python environment (.venv) and installs the requirements into it
#   4. adds an `sdr` command in %USERPROFILE%\.automated-sdr\bin and puts that folder on your PATH
#   5. starts `sdr setup` (a few friendly questions)
#
# Options (set them before running, e.g.  $env:SDR_NO_SETUP = '1'):
#   SDR_HOME      where to install                   (default: %USERPROFILE%\automated-sdr)
#   SDR_REPO      git repository to install from     (default: the official repo)
#   SDR_PYTHON    full path of a Python 3.11+ python.exe to use
#   SDR_NO_SETUP  '1' = install only; run `sdr setup` yourself later (useful for AI agents / CI)
#
# When it finishes, $LASTEXITCODE is 1 if the install failed; otherwise 0, or `sdr setup`'s own
# exit code when setup ran (for scripts and AI agents).
#
# Written to be piped into `iex`: everything runs inside one script block, so a download that is
# cut off can't run half an installer, and nothing calls `exit` (which would close your window).

& {
    Set-StrictMode -Version 2
    $ErrorActionPreference = 'Stop'

    $DefaultRepo = 'https://github.com/Fred-In-tech/automated-sdr-agent.git'
    $MinPython = '3.11'

    function Write-Banner {
        $art = @'

   _    _   _  _____   ___   __  __    _   _____  ___  ___    ___  ___   ___
  /_\  | | | ||_   _| / _ \ |  \/  |  /_\ |_   _|| __||   \  / __||   \ | _ \
 / _ \ | |_| |  | |  | (_) || |\/| | / _ \  | |  | _| | |) | \__ \| |) ||   /
/_/ \_\ \___/   |_|   \___/ |_|  |_|/_/ \_\ |_|  |___||___/  |___/|___/ |_|_\
'@
        $width = 80
        try { $width = $Host.UI.RawUI.WindowSize.Width } catch { $width = 80 }
        if ($width -ge 78) { Write-Host $art -ForegroundColor Cyan }
        else { Write-Host ''; Write-Host '  AUTOMATED SDR' -ForegroundColor Cyan }
        Write-Host '  by Fred  -  your AI sales rep, installed in about two minutes.'
    }

    function Write-Step([string]$Text) {
        Write-Host ''
        Write-Host "==> $Text" -ForegroundColor Cyan
    }
    function Write-Ok([string]$Text) { Write-Host "  [ok] $Text" -ForegroundColor Green }
    function Write-Note([string]$Text) { Write-Host "  [!] $Text" -ForegroundColor Yellow }
    function Write-Fail([string]$Text) { Write-Host "  [x] $Text" -ForegroundColor Red }
    function Write-Plain([string]$Text) { Write-Host $Text }

    function Get-PythonPath([string]$Exe, [string[]]$PyArgs) {
        # Returns the full path of the interpreter when "$Exe $PyArgs" is Python 3.11+, else $null.
        # The Microsoft Store "python.exe" alias only prints an install hint and fails, so it is
        # rejected here like any other unsuitable Python. Stdin is closed ($null |) so a launcher
        # that offers to download a missing version can never sit waiting for an answer.
        if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
        $ErrorActionPreference = 'Continue'
        $code = 'import sys; sys.exit(1) if sys.version_info < (3, 11) else print(sys.executable)'
        try {
            $out = $null | & $Exe @PyArgs -c $code 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ([string](@($out)[-1])).Trim() }
        } catch { }
        return $null
    }

    function Find-Python {
        # An explicit SDR_PYTHON is used as-is or rejected - never silently swapped for another.
        # `py -3` is the newest Python 3 the Windows launcher knows; the pinned versions only
        # matter when someone's default is older than 3.11 but a newer one is also installed.
        if ($env:SDR_PYTHON) { return (Get-PythonPath $env:SDR_PYTHON @()) }
        $candidates = @(
            @{ Exe = 'py'; Args = @('-3') },
            @{ Exe = 'py'; Args = @('-3.13') },
            @{ Exe = 'py'; Args = @('-3.12') },
            @{ Exe = 'py'; Args = @('-3.11') },
            @{ Exe = 'python'; Args = @() },
            @{ Exe = 'python3'; Args = @() }
        )
        foreach ($c in $candidates) {
            $path = Get-PythonPath $c.Exe $c.Args
            if ($path) { return $path }
        }
        return $null
    }

    function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$Failure) {
        # Native tools report failure through their exit code. With 'Stop', Windows PowerShell 5.1
        # turns any stderr line (git progress, pip warnings) into an exception when output is
        # redirected, e.g. when an AI agent runs this installer - so trust the exit code instead.
        # Output goes to the screen, never into a function's return value.
        $ErrorActionPreference = 'Continue'
        & $Exe @Arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw $Failure }
    }

    function Get-FullPath([string]$Path) {
        # Relative paths are resolved against PowerShell's current folder (not .NET's, which can
        # differ), and a leading ~ is expanded because it stays literal inside quotes.
        if ($Path -eq '~') { $Path = $env:USERPROFILE }
        elseif ($Path.StartsWith('~\') -or $Path.StartsWith('~/')) { $Path = Join-Path $env:USERPROFILE $Path.Substring(2) }
        return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path).TrimEnd('\')
    }

    function Get-NewestRelease([string]$Dir) {
        # The newest release tag (vX.Y.Z, compared numerically so v1.10.0 beats v1.9.0) in the
        # clone, or $null before the first release. Pre-release tags such as v2.0.0-rc1 are
        # ignored, exactly as core/updater.py does, so the installer and `sdr update` agree.
        $ErrorActionPreference = 'Continue'
        $tags = @(& git -C $Dir tag --list --sort=-v:refname 'v*' 2>$null)
        foreach ($tag in $tags) {
            if ("$tag".Trim() -match '^v\d+\.\d+\.\d+$') { return "$tag".Trim() }
        }
        return $null
    }

    function Get-CodeFolder([string]$Repo, [string]$Dir) {
        # Clone (or fetch into an existing clone), then move to the newest release tag: an install
        # must never run unreleased code from main (SECURITY.md promises release tags only, and
        # `sdr update` only ever moves between them). Before the first release there is no tag, so
        # the default branch is used. A failed move (offline, local edits) keeps the current version
        # instead of aborting: `sdr update` handles that safely, with backup and rollback.
        if (Test-Path -LiteralPath (Join-Path $Dir '.git')) {
            $ErrorActionPreference = 'Continue'
            & git -C $Dir fetch --tags --quiet origin | Out-Host
            if ($LASTEXITCODE -ne 0) {
                Write-Note "Couldn't check $Dir for new releases (offline?). Keeping the version you have."
                return
            }
            $tag = Get-NewestRelease $Dir
            if (-not $tag) {
                & git -C $Dir pull --ff-only --quiet | Out-Host
                if ($LASTEXITCODE -eq 0) { Write-Ok "Updated the existing copy in $Dir (no release yet, so it follows main)" }
                else { Write-Note "Couldn't update $Dir automatically (local changes?). Keeping the version you have." }
                return
            }
            & git -C $Dir checkout --quiet $tag | Out-Host
            if ($LASTEXITCODE -eq 0) { Write-Ok "Updated the existing copy in $Dir to release $tag" }
            else { Write-Note "Couldn't move $Dir to release $tag (local changes?). Keeping the version you have." }
            return
        }
        if ((Test-Path -LiteralPath $Dir) -and @(Get-ChildItem -Force -LiteralPath $Dir -ErrorAction SilentlyContinue).Count -gt 0) {
            throw "$Dir already exists and isn't an Automated SDR install. Move it away or pick another folder: `$env:SDR_HOME = 'C:\some\new\folder'"
        }
        Invoke-Checked 'git' @('clone', '--quiet', $Repo, $Dir) "Couldn't download $Repo. Check your internet connection (and access to the repository), then try again."
        $tag = Get-NewestRelease $Dir
        if ($tag) {
            Invoke-Checked 'git' @('-C', $Dir, 'checkout', '--quiet', $tag) "Couldn't check out release $tag in $Dir."
            Write-Ok "Downloaded release $tag into $Dir"
        } else {
            Write-Ok "Downloaded into $Dir"
            Write-Note 'No release has been tagged yet, so this copy follows the main branch until the first one.'
        }
    }

    function New-Venv([string]$Python, [string]$Dir) {
        # A project-private .venv keeps our requirements away from other Python projects. A broken
        # one (e.g. its Python was uninstalled) is rebuilt instead of reused.
        $venvPython = Join-Path $Dir '.venv\Scripts\python.exe'
        if ((Test-Path -LiteralPath $venvPython) -and (Get-PythonPath $venvPython @())) {
            Write-Ok "Using the existing environment ($Dir\.venv)"
            return $venvPython
        }
        Invoke-Checked $Python @('-m', 'venv', '--clear', (Join-Path $Dir '.venv')) "Couldn't create a Python environment with $Python."
        Write-Ok "Created $Dir\.venv"
        return $venvPython
    }

    function Get-OemEncoding {
        # cmd.exe reads .cmd files in the console's OEM code page; fall back to ASCII if unavailable.
        try {
            return [System.Text.Encoding]::GetEncoding([System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
        } catch {
            return [System.Text.Encoding]::ASCII
        }
    }

    function Get-ShimTarget([string]$Shim) {
        # The bin\sdr.cmd an existing shim hands over to (%USERPROFILE% expanded), or $null.
        if (-not (Test-Path -LiteralPath $Shim)) { return $null }
        foreach ($line in [System.IO.File]::ReadAllLines($Shim)) {
            if ($line -match '^"(.+)" %\*$') { return [Environment]::ExpandEnvironmentVariables($Matches[1]) }
        }
        return $null
    }

    function Install-Shim([string]$Dir, [string]$ShimDir) {
        # A two-line sdr.cmd on PATH that hands over to bin\sdr.cmd in the install folder, so updates
        # never need the shim rewritten. %USERPROFILE% is kept symbolic to survive non-ASCII names.
        # A shim that still starts a different, existing install (a second copy for another business)
        # is someone's working command and is left alone. Returns the command to run.
        New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
        $target = Join-Path $Dir 'bin\sdr.cmd'
        $shim = Join-Path $ShimDir 'sdr.cmd'
        $existing = Get-ShimTarget $shim
        if ($existing -and ($existing -ine $target) -and (Test-Path -LiteralPath $existing)) {
            Write-Note "$shim already starts another install ($existing), so it was left alone. Use $target instead."
            return $target
        }
        if ($target.StartsWith($env:USERPROFILE + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            $target = '%USERPROFILE%' + $target.Substring($env:USERPROFILE.Length)
        }
        $content = "@echo off`r`n`"$target`" %*`r`n"
        [System.IO.File]::WriteAllText($shim, $content, (Get-OemEncoding))
        Write-Ok "Added the sdr command: $shim"
        return $shim
    }

    function Add-ToUserPath([string]$Folder) {
        # Read and write the raw registry value so entries like %USERPROFILE%\... stay unexpanded
        # (Environment.SetEnvironmentVariable would flatten them). Setting and clearing a dummy
        # user variable afterwards broadcasts the change, so new terminals pick it up.
        $inSession = @($env:Path -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ieq $Folder) }).Count -gt 0
        if (-not $inSession) { $env:Path = "$env:Path;$Folder" }

        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        try {
            $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $entries = @($current -split ';' | Where-Object { $_ })
            foreach ($entry in $entries) {
                if ([Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\') -ieq $Folder) { return $false }
            }
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            if ($null -ne $key.GetValue('Path')) { $kind = $key.GetValueKind('Path') }
            $key.SetValue('Path', ((@($entries) + $Folder) -join ';'), $kind)
        } finally {
            $key.Close()
        }
        [Environment]::SetEnvironmentVariable('AUTOMATED_SDR_PATH_REFRESH', '1', 'User')
        [Environment]::SetEnvironmentVariable('AUTOMATED_SDR_PATH_REFRESH', $null, 'User')
        return $true
    }

    function Install-AutomatedSdr {
        Write-Banner

        if ($PSVersionTable.PSVersion.Major -lt 5) {
            throw 'PowerShell 5.1 or newer is required. Install it from https://aka.ms/powershell'
        }

        $installDir = Get-FullPath $(if ($env:SDR_HOME) { $env:SDR_HOME } else { Join-Path $env:USERPROFILE 'automated-sdr' })
        $repo = if ($env:SDR_REPO) { $env:SDR_REPO } else { $DefaultRepo }
        $shimDir = Join-Path $env:USERPROFILE '.automated-sdr\bin'

        Write-Step '1/5  Checking what you need'
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-Fail 'Git is not installed. Install it with:'
            Write-Plain '    winget install --id Git.Git -e --source winget'
            Write-Plain '    (or download it from https://git-scm.com/download/win)'
            throw 'Then open a NEW PowerShell window and run this installer again.'
        }
        Write-Ok ((& git --version) -join ' ')

        $python = Find-Python
        if (-not $python) {
            if ($env:SDR_PYTHON) { throw "SDR_PYTHON=$env:SDR_PYTHON is not Python $MinPython or newer." }
            Write-Fail "Python $MinPython or newer is required. Install it with:"
            Write-Plain '    winget install --id Python.Python.3.12 -e --source winget'
            Write-Plain '    (or download it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH")'
            throw 'Then open a NEW PowerShell window and run this installer again.'
        }
        Write-Ok "Python ($python)"

        Write-Step '2/5  Getting Automated SDR'
        Get-CodeFolder $repo $installDir

        Write-Step '3/5  Creating a private Python environment'
        $venvPython = New-Venv $python $installDir

        Write-Step '4/5  Installing requirements (about a minute)'
        Invoke-Checked $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', (Join-Path $installDir 'requirements.txt')) 'Installing the requirements failed. Check your internet connection and run the installer again.'
        Write-Ok 'Requirements installed'

        Write-Step '5/5  Adding the sdr command'
        $sdr = Install-Shim $installDir $shimDir
        try {
            if (Add-ToUserPath $shimDir) { Write-Ok "Added $shimDir to your PATH (new terminals will find 'sdr')" }
            else { Write-Ok "$shimDir is already on your PATH" }
        } catch {
            Write-Note "Couldn't add $shimDir to your PATH automatically. Add it in Settings > 'Edit environment variables for your account', or run $sdr directly."
        }

        Write-Host ''
        Write-Host "  Installed! Automated SDR lives in $installDir" -ForegroundColor Green
        Write-Plain '  Useful commands:  sdr setup  |  sdr preview  |  sdr dashboard  |  sdr doctor'

        if ($env:SDR_NO_SETUP -eq '1') {
            Write-Plain '  Setup skipped (SDR_NO_SETUP=1). When you are ready, run:  sdr setup'
            $global:LASTEXITCODE = 0
            return
        }
        Write-Step 'Starting setup'
        # Not piped anywhere: setup needs the real console for its arrow-key menus. Its exit code
        # becomes this installer's $LASTEXITCODE.
        $ErrorActionPreference = 'Continue'
        & $sdr setup
    }

    try {
        Install-AutomatedSdr
    } catch {
        Write-Fail $_.Exception.Message
        $global:LASTEXITCODE = 1
    }
}
