"""Sleeper clients: documented v1 API and the undocumented projections/stats endpoints.

Every method returns raw bytes — the caller snapshots first, parses second.
"""

from __future__ import annotations

from collections.abc import Sequence

from lazy_sleeper.ingest.http import HttpClient

V1 = "https://api.sleeper.app/v1"
PROJ = "https://api.sleeper.com"
POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")


class SleeperClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    # --- documented ---------------------------------------------------------
    def league(self, league_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}")

    def league_users(self, league_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}/users")

    def league_rosters(self, league_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}/rosters")

    def league_drafts(self, league_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}/drafts")

    def league_transactions(self, league_id: str, week: int) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}/transactions/{week}")

    def league_matchups(self, league_id: str, week: int) -> bytes:
        return self._http.get_bytes(f"{V1}/league/{league_id}/matchups/{week}")

    def draft(self, draft_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/draft/{draft_id}")

    def draft_picks(self, draft_id: str) -> bytes:
        return self._http.get_bytes(f"{V1}/draft/{draft_id}/picks")

    def players(self) -> bytes:
        # Full player map (several MB). Pull daily at most.
        return self._http.get_bytes(f"{V1}/players/nfl")

    def trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 50) -> bytes:
        return self._http.get_bytes(
            f"{V1}/players/nfl/trending/{kind}",
            params={"lookback_hours": lookback_hours, "limit": limit},
        )

    def state(self) -> bytes:
        return self._http.get_bytes(f"{V1}/state/nfl")

    # --- undocumented (projections + ADP, stats) -----------------------------
    def projections(
        self, season: int, week: int | None = None, positions: Sequence[str] = POSITIONS
    ) -> bytes:
        return self._http.get_bytes(
            self._proj_url("projections", season, week), params=self._pos_params(positions)
        )

    def stats(
        self, season: int, week: int | None = None, positions: Sequence[str] = POSITIONS
    ) -> bytes:
        return self._http.get_bytes(
            self._proj_url("stats", season, week), params=self._pos_params(positions)
        )

    @staticmethod
    def _proj_url(kind: str, season: int, week: int | None) -> str:
        base = f"{PROJ}/{kind}/nfl/{season}"
        return f"{base}/{week}" if week is not None else base

    @staticmethod
    def _pos_params(positions: Sequence[str]) -> list[tuple[str, object]]:
        params: list[tuple[str, object]] = [("season_type", "regular")]
        params += [("position[]", p) for p in positions]
        return params
