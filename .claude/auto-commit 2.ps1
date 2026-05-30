# =====================================================
# Auto-commit + push helper for the Personal AI OS repo.
# Invoked by the Stop hook after each Claude turn.
#
# Behaviour:
#   - Stages everything that's tracked or new and not gitignored
#   - Commits ONLY if there's something staged
#   - Pushes ONLY if there's an upstream branch
#   - Always exits 0 so it never blocks Claude
# =====================================================

$ErrorActionPreference = "Continue"

try {
    Set-Location -Path $PSScriptRoot
    Set-Location -Path ".."

    # Stage everything (gitignore protects .env / token.json / data/ / logs/).
    git add -A 2>$null | Out-Null

    # Anything staged?
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        # No changes — nothing to commit, exit silently.
        exit 0
    }

    git commit -m "auto: snapshot from claude session" --quiet 2>$null | Out-Null

    # Push only if an upstream is configured. Suppress all noise.
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>$null
    if ($LASTEXITCODE -eq 0 -and $upstream) {
        git push --quiet 2>$null | Out-Null
    }
} catch {
    # Swallow everything — never block the Stop hook.
}

exit 0
