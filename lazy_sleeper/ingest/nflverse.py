"""nflverse / ffverse GitHub release assets + dynastyprocess crosswalk. Returns raw bytes.

Asset names verified 2026-08-16 against the live releases:
  nflverse-data  tag `stats_player`  → stats_player_week_{season}.csv  (weekly; _reg_ = totals)
  nflverse-data  tag `snap_counts`   → snap_counts_{season}.csv
  ffopportunity  tag `latest-data`   → ep_weekly_{season}.csv          (xFP / expected points)
  dynastyprocess/data                → files/db_playerids.csv           (crosswalk)
"""

from __future__ import annotations

from lazy_sleeper.ingest.http import HttpClient

NFLVERSE_RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
FFOPP_RELEASES = "https://github.com/ffverse/ffopportunity/releases/download"
CROSSWALK_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv"


class NflverseClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def release_asset(self, tag: str, filename: str, *, releases: str = NFLVERSE_RELEASES) -> bytes:
        return self._http.get_bytes(f"{releases}/{tag}/{filename}")

    def stats_player_week(self, season: int, fmt: str = "csv") -> bytes:
        return self.release_asset("stats_player", f"stats_player_week_{season}.{fmt}")

    def snap_counts(self, season: int, fmt: str = "csv") -> bytes:
        return self.release_asset("snap_counts", f"snap_counts_{season}.{fmt}")

    def ff_opportunity(self, season: int, fmt: str = "csv") -> bytes:
        return self.release_asset(
            "latest-data", f"ep_weekly_{season}.{fmt}", releases=FFOPP_RELEASES
        )

    def crosswalk(self) -> bytes:
        return self._http.get_bytes(CROSSWALK_URL)
