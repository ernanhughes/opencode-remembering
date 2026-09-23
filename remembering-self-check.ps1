param(
    [string]$Project = "",
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$PluginRoot = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Project)) {
    $Project = $PluginRoot
}

$script:Failures = 0
$script:Warnings = 0

function Write-Result {
    param(
        [ValidateSet("PASS","WARN","FAIL","INFO")]
        [string]$Status,
        [string]$Name,
        [string]$Detail = ""
    )

    switch ($Status) {
        "PASS" { $prefix = "[PASS]"; $scriptColor = "Green" }
        "WARN" { $prefix = "[WARN]"; $scriptColor = "Yellow"; $script:Warnings++ }
        "FAIL" { $prefix = "[FAIL]"; $scriptColor = "Red"; $script:Failures++ }
        default { $prefix = "[INFO]"; $scriptColor = "Cyan" }
    }

    Write-Host ("{0,-7} {1}" -f $prefix, $Name) -ForegroundColor $scriptColor
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        Write-Host ("         {0}" -f $Detail) -ForegroundColor DarkGray
    }
}

function Safe-Version {
    param(
        [System.Management.Automation.CommandInfo]$Command,
        [string[]]$ArgumentList
    )
    try {
        $output = @(& $Command.Source @ArgumentList 2>&1)
        if ($LASTEXITCODE -eq 0 -and $output.Count -gt 0) {
            return (($output | ForEach-Object { $_.ToString() }) -join " ").Trim()
        }
    } catch {}
    return $null
}

Clear-Host
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " OpenCode Remembering - Self Check" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Plugin:  $PluginRoot"
Write-Host "Project: $Project"
Write-Host ""

if (Test-Path -LiteralPath $PluginRoot) {
    Write-Result PASS "Plugin directory exists" $PluginRoot
} else {
    Write-Result FAIL "Plugin directory missing" $PluginRoot
}

$packagePath = Join-Path $PluginRoot "package.json"
$pluginSource = Join-Path $PluginRoot "src\plugin.ts"
$nativeCli = Join-Path $PluginRoot "src\native-cli.ts"
$distIndex = Join-Path $PluginRoot "dist\index.js"

if (Test-Path -LiteralPath $packagePath) {
    try {
        $pkg = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json
        if ($pkg.name -eq "opencode-remembering") {
            Write-Result PASS "Package identity" ("{0} v{1}" -f $pkg.name, $pkg.version)
        } else {
            Write-Result FAIL "Package identity" ("Expected opencode-remembering, found: {0}" -f $pkg.name)
        }
    } catch {
        Write-Result FAIL "package.json readable" $_.Exception.Message
    }
} else {
    Write-Result FAIL "package.json present" $packagePath
}

if (Test-Path -LiteralPath $pluginSource) {
    $pluginText = Get-Content -LiteralPath $pluginSource -Raw
    if ($pluginText -match 'id\s*:\s*["'']opencode-remembering["'']') {
        Write-Result PASS "OpenCode plugin ID" "opencode-remembering"
    } else {
        Write-Result FAIL "OpenCode plugin ID" "src\plugin.ts does not declare id: opencode-remembering"
    }
} else {
    Write-Result FAIL "Plugin source present" $pluginSource
}

$noPythonGuard = Join-Path $PluginRoot "src\no-python-runtime.test.ts"
if (Test-Path -LiteralPath $noPythonGuard) {
    Write-Result PASS "Native JS runtime guard" "no-python-runtime.test.ts is present"
} else {
    Write-Result WARN "Native JS runtime guard" "no-python-runtime.test.ts was not found"
}

if (Test-Path -LiteralPath $nativeCli) {
    Write-Result PASS "Native engine CLI present" "src\native-cli.ts"
} else {
    Write-Result FAIL "Native engine CLI present" $nativeCli
}

$bun = Get-Command bun -ErrorAction SilentlyContinue
if ($bun) {
    $bunVersion = Safe-Version $bun @("--version")
    if ($bunVersion) {
        Write-Result PASS "Bun available" $bunVersion
    } else {
        Write-Result PASS "Bun available" $bun.Source
    }
} else {
    Write-Result FAIL "Bun available" "Install Bun or add it to PATH"
}

$opencode = Get-Command opencode -ErrorAction SilentlyContinue
if ($opencode) {
    $ocVersion = Safe-Version $opencode @("--version")
    if ($ocVersion) {
        Write-Result PASS "OpenCode available" $ocVersion
    } else {
        Write-Result PASS "OpenCode available" $opencode.Source
    }
} else {
    Write-Result WARN "OpenCode available" "opencode was not found on PATH"
}

if (Test-Path -LiteralPath $distIndex) {
    Write-Result PASS "Compiled plugin present" "dist\index.js"
} else {
    Write-Result WARN "Compiled plugin present" "dist\index.js is missing; run: bun run build"
}

$configCandidates = @(
    (Join-Path $HOME ".config\opencode\opencode.json"),
    (Join-Path $HOME ".config\opencode\opencode.jsonc"),
    (Join-Path $HOME ".opencode\opencode.json"),
    (Join-Path $HOME ".opencode\opencode.jsonc"),
    (Join-Path $Project "opencode.json"),
    (Join-Path $Project "opencode.jsonc")
)

if ($env:APPDATA) {
    $configCandidates += (Join-Path $env:APPDATA "opencode\opencode.json")
    $configCandidates += (Join-Path $env:APPDATA "opencode\opencode.jsonc")
}

$configCandidates = $configCandidates | Select-Object -Unique
$foundConfig = $false
$foundReference = $false
$foundAt = @()

foreach ($candidate in $configCandidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }

    $foundConfig = $true
    try {
        $text = Get-Content -LiteralPath $candidate -Raw
        $normalizedRoot = $PluginRoot.Replace("\", "/")
        if (
            $text -match 'opencode-remembering' -or
            $text.Contains($PluginRoot) -or
            $text.Contains($normalizedRoot)
        ) {
            $foundReference = $true
            $foundAt += $candidate
        }
    } catch {}
}

if ($foundReference) {
    Write-Result PASS "OpenCode configuration references plugin" ($foundAt -join "; ")
} elseif ($foundConfig) {
    Write-Result WARN "OpenCode configuration references plugin" "Config found, but no obvious opencode-remembering reference was detected"
} else {
    Write-Result WARN "OpenCode configuration located" "No common OpenCode config file was found. The plugin may still be configured elsewhere."
}

if ($bun -and (Test-Path -LiteralPath $nativeCli)) {
    Write-Host ""
    Write-Host "Running native memory health check..." -ForegroundColor Cyan

    Push-Location $PluginRoot
    try {
        $doctorLines = @(& $bun.Source "src/native-cli.ts" "doctor" "--dir" $Project 2>&1)
        $doctorExit = $LASTEXITCODE
        $doctorText = ($doctorLines | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine

        if ($doctorExit -eq 0) {
            Write-Result PASS "Remembering native doctor" "PostgreSQL/config/embedding readiness check completed successfully"
        } else {
            Write-Result FAIL "Remembering native doctor" ("Exit code {0}" -f $doctorExit)
        }

        try {
            $doctor = $doctorText | ConvertFrom-Json
            if ($null -ne $doctor.ok) {
                if ([bool]$doctor.ok) {
                    Write-Result PASS "Engine reports healthy" "ok=true"
                } else {
                    $message = if ($doctor.message) { [string]$doctor.message } else { "ok=false" }
                    Write-Result FAIL "Engine reports healthy" $message
                }
            }

            if ($doctor.schema) {
                Write-Result INFO "Project schema" ([string]$doctor.schema)
            }

            if ($doctor.dsn_redacted) {
                Write-Result INFO "Database endpoint" ([string]$doctor.dsn_redacted)
            }

            if ($doctor.readiness -and $doctor.readiness.code) {
                Write-Result INFO "Readiness" ([string]$doctor.readiness.code)
            }

            if ($doctor.embedding) {
                $provider = $doctor.embedding.provider
                $model = $doctor.embedding.model
                if ($provider -or $model) {
                    Write-Result INFO "Embedding" ("{0} / {1}" -f $provider, $model)
                }
            }

            if ($doctor.indexed -eq $false) {
                Write-Result WARN "Memory index" "Project is not indexed yet. Run memory_setup or bun run setup."
            }
        } catch {
            if ($doctorExit -ne 0) {
                $tail = ($doctorLines | Select-Object -Last 8) -join [Environment]::NewLine
                if ($tail) {
                    Write-Host $tail -ForegroundColor DarkGray
                }
            }
        }
    } catch {
        Write-Result FAIL "Remembering native doctor" $_.Exception.Message
    } finally {
        Pop-Location
    }
}

if ($Full -and $bun) {
    Write-Host ""
    Write-Host "Running full plugin check..." -ForegroundColor Cyan
    Push-Location $PluginRoot
    try {
        & $bun.Source run check
        if ($LASTEXITCODE -eq 0) {
            Write-Result PASS "Full plugin test/check suite" "bun run check"
        } else {
            Write-Result FAIL "Full plugin test/check suite" ("Exit code {0}" -f $LASTEXITCODE)
        }
    } catch {
        Write-Result FAIL "Full plugin test/check suite" $_.Exception.Message
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan

if ($script:Failures -eq 0) {
    Write-Host " REMEMBERING READY" -ForegroundColor Green
    if ($script:Warnings -gt 0) {
        Write-Host (" {0} warning(s) - review above, but no hard failure." -f $script:Warnings) -ForegroundColor Yellow
    } else {
        Write-Host " All checks passed." -ForegroundColor Green
    }
    $exitCode = 0
} else {
    Write-Host " REMEMBERING NOT READY" -ForegroundColor Red
    Write-Host (" {0} failure(s), {1} warning(s)." -f $script:Failures, $script:Warnings) -ForegroundColor Red
    $exitCode = 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Inside OpenCode, memory_health / memory_context / memory_trace"
Write-Host "confirm that the live plugin tool surface has registered."
Write-Host ""

exit $exitCode
