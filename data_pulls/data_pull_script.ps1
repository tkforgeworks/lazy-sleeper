$ProgressPreference = 'SilentlyContinue'   # big speedup for Invoke-WebRequest loops
$stamp = Get-Date -Format 'yyyy-MM-dd'
$dir = "ff-projections-$stamp"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$pos = 'position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF'
$espnHeaders = @{ 'X-Fantasy-Filter' = '{"players":{"limit":2000,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}' }

# --- 1) Sleeper season-long projections: 2024 + 2025 (benchmarks), 2026 (draft fuel) ---
foreach ($yr in 2024, 2025, 2026) {
    Invoke-WebRequest -Uri "https://api.sleeper.com/projections/nfl/$($yr)?season_type=regular&$pos&order_by=ppr" `
        -OutFile (Join-Path $dir "sleeper_proj_$($yr)_season.json")
}

# --- 2) Sleeper weekly projections, weeks 1-18 for 2024 + 2025 (the accuracy benchmark set) ---
foreach ($yr in 2024, 2025) {
    foreach ($wk in 1..18) {
        Invoke-WebRequest -Uri "https://api.sleeper.com/projections/nfl/$($yr)/$($wk)?season_type=regular&$pos" `
            -OutFile (Join-Path $dir ("sleeper_proj_{0}_wk{1:d2}.json" -f $yr, $wk))
        Start-Sleep -Milliseconds 250   # be polite to the API
    }
}

# --- 3) ESPN kona (projections + actuals in one payload): 2024, 2025, 2026 ---
foreach ($yr in 2024, 2025, 2026) {
    Invoke-WebRequest -Uri "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/$($yr)/segments/0/leaguedefaults/3?view=kona_player_info" `
        -Headers $espnHeaders -OutFile (Join-Path $dir "espn_kona_$($yr).json")
}

# --- 4) Review sizes, then zip for upload ---
Get-ChildItem $dir | Sort-Object Name | Format-Table Name, @{n = 'KB'; e = { [math]::Round($_.Length / 1KB) } }
Compress-Archive -Path (Join-Path $dir '*') -DestinationPath "$dir.zip" -Force
Write-Host "Upload this file: $((Resolve-Path "$dir.zip").Path)"