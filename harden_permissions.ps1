# Tighten the ACLs on this bot's credential files.
#
# Run this in an **Administrator** PowerShell, from the repository folder:
#
#     cd "D:\TG_Bot\twitter_pixiv_tg_bot_windows"
#     powershell -ExecutionPolicy Bypass -File harden_permissions.ps1
#
# Why this is needed
# ------------------
# On a default Windows profile the repository directory grants
#
#     NT AUTHORITY\Authenticated Users  (I)(M)   <- Modify, inherited
#     BUILTIN\Users                     (I)(RX)  <- Read,   inherited
#
# so any other local account - or any process running as another user - can read `.env`
# (the bot token) and write `parse_cache.json` and `.cache/`. The cache findings in the
# security audit are only reachable *because* that write access exists: a crafted cache
# index could name `../.env` and have the bot upload it. Removing the write access takes
# the whole class away rather than relying on the validation alone.
#
# What it does
# ------------
# For each credential file and directory: disables inheritance, removes every inherited
# entry, and grants full control to the current user, SYSTEM and Administrators only.
#
# It is written with Get-Acl/Set-Acl rather than icacls because icacls' /grant:r parses
# "(R,W)" inconsistently - it reports "invalid parameter" and changes nothing while still
# printing "Successfully processed", which reads like success.

$ErrorActionPreference = 'Stop'

# Verify we are elevated; without it the changes silently fail for a protected file.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This script must run in an Administrator PowerShell." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as administrator, then run it again."
    exit 1
}

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo
Write-Host "Repository: $repo"
Write-Host "User      : $($identity.Name)"
Write-Host

$targets = @(
    '.env',
    'authorized.json',
    'parse_cache.json',
    'gallery-dl.conf',
    'cookies',
    '.cache'
)

# Full control for these three; nothing else keeps any access.
$keep = @(
    $identity.Name,
    'NT AUTHORITY\SYSTEM',
    'BUILTIN\Administrators'
)

foreach ($target in $targets) {
    $path = Join-Path $repo $target
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host ("  skip   {0}  (does not exist)" -f $target) -ForegroundColor DarkGray
        continue
    }

    try {
        $acl = Get-Acl -LiteralPath $path
        # Disable inheritance and drop everything that was inherited.
        $acl.SetAccessRuleProtection($true, $false)

        # Build the replacement rules BEFORE removing anything.
        #
        # Order matters and this is not hypothetical: an earlier version removed every
        # rule first and only then constructed the new ones. Constructing a rule for a
        # FILE with inheritance flags throws - "No flags can be set" - so the removal
        # succeeded, every addition failed, and the file was left with NO access for
        # anyone. It had to be deleted by hand. Build first, verify you have rules, and
        # only then touch the ACL.
        #
        # Inheritance flags are valid on a directory and invalid on a file, which is why
        # `isContainer` decides them.
        $isContainer = (Get-Item -LiteralPath $path).PSIsContainer
        $inheritFlags = if ($isContainer) {
            [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        } else {
            [Security.AccessControl.InheritanceFlags]::None
        }

        $newRules = @()
        foreach ($name in $keep) {
            try {
                $newRules += New-Object Security.AccessControl.FileSystemAccessRule(
                    $name,
                    [Security.AccessControl.FileSystemRights]::FullControl,
                    $inheritFlags,
                    [Security.AccessControl.PropagationFlags]::None,
                    [Security.AccessControl.AccessControlType]::Allow)
            }
            catch {
                Write-Host ("         could not build a rule for {0}: {1}" -f $name, $_.Exception.Message) -ForegroundColor Yellow
            }
        }

        if ($newRules.Count -eq 0) {
            Write-Host ("  SKIPPED {0}: no usable rule could be built, so the ACL is left untouched" -f $target) -ForegroundColor Red
            continue
        }

        # Only now is it safe to clear the existing rules.
        foreach ($rule in @($acl.Access)) {
            [void]$acl.RemoveAccessRule($rule)
        }
        foreach ($rule in $newRules) {
            [void]$acl.AddAccessRule($rule)
        }

        Set-Acl -LiteralPath $path -AclObject $acl

        # Read it back: "no error" is not the same as "the access is right", and a file
        # left with no rules cannot even be inspected by its owner.
        $check = Get-Acl -LiteralPath $path
        if (@($check.Access).Count -eq 0) {
            Write-Host ("  WARNING {0}: ACL ended up empty - restoring inheritance" -f $target) -ForegroundColor Red
            $restore = Get-Acl -LiteralPath $path
            $restore.SetAccessRuleProtection($false, $true)
            Set-Acl -LiteralPath $path -AclObject $restore
            continue
        }

        Write-Host ("  fixed  {0}  ({1} rule(s))" -f $target, @($check.Access).Count) -ForegroundColor Green
    }
    catch {
        Write-Host ("  FAILED {0}: {1}" -f $target, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host
Write-Host "Result:"
foreach ($target in $targets) {
    $path = Join-Path $repo $target
    if (Test-Path -LiteralPath $path) {
        Write-Host ("--- {0} ---" -f $target)
        icacls $path
    }
}

Write-Host
Write-Host "Ensure only the named accounts appear above, with no 'Authenticated Users'." -ForegroundColor Cyan
Write-Host "If you ever need to undo this, re-enable inheritance with:"
Write-Host "  icacls <file> /inheritance:e"
