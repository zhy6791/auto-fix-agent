# PowerShell demo script for AutoFixAgent
# Usage: .\scripts\demo.ps1 -DryRun    # Analyze only
#        .\scripts\demo.ps1 -AutoApply # Apply patch to working tree

param(
    [switch]$DryRun = $false,
    [switch]$AutoApply = $false,
    [switch]$Verbose = $false,
    [string]$Config = "configs/config.yml"
)

# Colors for console output
$Colors = @{
    Info    = "Cyan"
    Success = "Green"
    Warning = "Yellow"
    Error   = "Red"
}

function Write-Log {
    param([string]$Message, [string]$Level = "Info")
    $Color = $Colors[$Level]
    Write-Host "[$Level] $Message" -ForegroundColor $Color
}

function Check-Prerequisites {
    Write-Log "Checking prerequisites..." "Info"

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Log "Python not found in PATH" "Error"
        exit 1
    }
    Write-Log "✓ Python found" "Success"

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Log "Git not found in PATH" "Error"
        exit 1
    }
    Write-Log "✓ Git found" "Success"

    if (-not (Test-Path $Config)) {
        Write-Log "Config file not found: $Config" "Error"
        exit 1
    }
    Write-Log "✓ Config file found" "Success"
}

function Install-Dependencies {
    Write-Log "Installing dependencies..." "Info"

    # Try uv first, fall back to pip
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Log "Using uv for installation" "Info"
        uv install
    } else {
        Write-Log "Using pip for installation (uv not found)" "Warning"
        pip install pyyaml requests openai gitpython
    }

    Write-Log "✓ Dependencies installed" "Success"
}

function Run-DryRun {
    Write-Log "Running in DRY-RUN mode (analysis only, no code changes)" "Warning"

    $Args = @("main.py")
    $Args += @("--config", $Config)
    $Args += @("--dry-run")

    if ($Verbose) {
        $Args += @("--verbose")
    }

    Write-Log "Command: python $($Args -join ' ')" "Info"
    Write-Log "" "Info"

    python -m @Args

    if ($LASTEXITCODE -eq 0) {
        Write-Log "✓ DRY-RUN completed successfully" "Success"
    } else {
        Write-Log "✗ DRY-RUN failed with exit code $LASTEXITCODE" "Error"
        exit 1
    }
}

function Run-AutoApply {
    Write-Log "Running in AUTO-APPLY mode (will create branch and modify files)" "Warning"

    # Get confirmation
    Write-Log "This will:" "Warning"
    Write-Log "  1. Create a local branch with name 'fix/auto-<timestamp>'" "Warning"
    Write-Log "  2. Modify files in the working tree" "Warning"
    Write-Log "  3. NOT commit or push changes" "Warning"
    Write-Log "" "Warning"

    $Confirm = Read-Host "Continue? (yes/no)"
    if ($Confirm -ne "yes") {
        Write-Log "Aborted" "Info"
        exit 0
    }

    $Args = @("main.py")
    $Args += @("--config", $Config)
    $Args += @("--auto-apply")

    if ($Verbose) {
        $Args += @("--verbose")
    }

    Write-Log "" "Info"
    Write-Log "Command: python $($Args -join ' ')" "Info"
    Write-Log "" "Info"

    python -m @Args

    if ($LASTEXITCODE -eq 0) {
        Write-Log "✓ AUTO-APPLY completed successfully" "Success"
        Write-Log "Check your repo for the new fix/ branch and modified files" "Info"
    } else {
        Write-Log "✗ AUTO-APPLY failed with exit code $LASTEXITCODE" "Error"
        exit 1
    }
}

# Main script
function Main {
    Write-Log "===== AutoFixAgent Demo =====" "Info"
    Write-Log "Config: $Config" "Info"
    Write-Log "Mode: $(if ($AutoApply) {'AUTO-APPLY'} else {'DRY-RUN'})" "Info"
    Write-Log "" "Info"

    Check-Prerequisites
    Write-Log "" "Info"

    Install-Dependencies
    Write-Log "" "Info"

    if ($AutoApply) {
        Run-AutoApply
    } else {
        Run-DryRun
    }

    Write-Log "" "Info"
    Write-Log "===== Demo Complete =====" "Success"
}

# Run main
Main

