# Lazy Sleeper — draft-night launcher. Double-click via a shortcut whose target is:
#   pwsh -NoExit -File C:\Code\lazy-sleeper\scripts\serve.ps1
# (right-click this file → Send to → Desktop to make one, then edit the shortcut target)
Set-Location (Split-Path $PSScriptRoot -Parent)
uv run lazy serve
