"""ESPN kona_player_info (undocumented). One payload = seasonal + weekly projections and actuals."""

from __future__ import annotations

import json

from lazy_sleeper.ingest.http import HttpClient

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"


class EspnClient:
    def __init__(self, http: HttpClient, *, limit: int = 2000) -> None:
        self._http = http
        self._limit = limit

    def kona(self, season: int) -> bytes:
        url = f"{BASE}/{season}/segments/0/leaguedefaults/3"
        fantasy_filter = {
            "players": {
                "limit": self._limit,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }
        headers = {"X-Fantasy-Filter": json.dumps(fantasy_filter, separators=(",", ":"))}
        return self._http.get_bytes(url, params={"view": "kona_player_info"}, headers=headers)
