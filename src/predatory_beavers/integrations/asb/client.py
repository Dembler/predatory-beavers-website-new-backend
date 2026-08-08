import json
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from predatory_beavers.integrations.asb.errors import (
    AsbDisabledError,
    AsbIdentifierNotAllowedError,
    AsbInvalidResponseError,
    AsbResponseTooLargeError,
    AsbUpstreamError,
)
from predatory_beavers.integrations.asb.schemas import AsbGame, AsbStanding
from predatory_beavers.settings import Settings

_games_adapter = TypeAdapter(list[AsbGame])
_standings_adapter = TypeAdapter(list[AsbStanding])


@dataclass(frozen=True, slots=True)
class AsbImportBundle:
    games: list[AsbGame]
    standings: list[AsbStanding]


class AsbClient(Protocol):
    async def fetch_import(
        self,
        *,
        competition_id: str,
        standings_competition_id: str,
        external_team_id: str,
    ) -> AsbImportBundle: ...


class HttpAsbClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._base_url = httpx.URL(settings.asb_base_url)

    async def fetch_import(
        self,
        *,
        competition_id: str,
        standings_competition_id: str,
        external_team_id: str,
    ) -> AsbImportBundle:
        if not self._settings.asb_enabled:
            raise AsbDisabledError
        self._require_allowed(
            competition_id=competition_id,
            standings_competition_id=standings_competition_id,
            external_team_id=external_team_id,
        )
        games_payload = await self._get_json(
            path=f"/Widget/TeamGames/{external_team_id}",
            params={"compId": competition_id, "format": "json"},
        )
        standings_payload = await self._get_json(
            path=f"/Widget/CompTeamResults/{standings_competition_id}",
            params={"format": "json"},
        )
        try:
            games = _games_adapter.validate_python(games_payload)
            standings = _standings_adapter.validate_python(standings_payload)
        except ValidationError as exc:
            raise AsbInvalidResponseError from exc
        if len(games) > self._settings.asb_max_games or len(standings) > 100:
            raise AsbInvalidResponseError("ASB response contains too many records")
        if any(str(row.competition_id) != standings_competition_id for row in standings):
            raise AsbInvalidResponseError("ASB standings contain an unexpected competition")
        if any(
            external_team_id not in {str(game.home_team_id), str(game.away_team_id)}
            for game in games
        ):
            raise AsbInvalidResponseError("ASB games contain an unexpected team")
        return AsbImportBundle(games=games, standings=standings)

    def _require_allowed(
        self,
        *,
        competition_id: str,
        standings_competition_id: str,
        external_team_id: str,
    ) -> None:
        competition_allowlist = set(self._settings.asb_allowed_competition_ids)
        if (
            competition_id not in competition_allowlist
            or standings_competition_id not in competition_allowlist
            or external_team_id not in set(self._settings.asb_allowed_team_ids)
        ):
            raise AsbIdentifierNotAllowedError

    async def _get_json(self, *, path: str, params: dict[str, str]) -> object:
        timeout = httpx.Timeout(self._settings.asb_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Predatory-Beavers-Backend/0.1",
                },
            ) as client:
                url = self._base_url.copy_with(path=path, query=None)
                async with client.stream("GET", url, params=params) as response:
                    if response.is_redirect:
                        raise AsbUpstreamError("ASB redirects are not allowed")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise AsbUpstreamError(f"ASB returned HTTP {response.status_code}")
                    content_type = response.headers.get("Content-Type", "")
                    if content_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise AsbInvalidResponseError("ASB returned a non-JSON response")
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            if int(content_length) > self._settings.asb_max_response_bytes:
                                raise AsbResponseTooLargeError
                        except ValueError:
                            raise AsbInvalidResponseError(
                                "ASB returned an invalid Content-Length"
                            ) from None
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._settings.asb_max_response_bytes:
                            raise AsbResponseTooLargeError
                        chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise AsbUpstreamError("ASB request timed out") from exc
        except httpx.RequestError as exc:
            raise AsbUpstreamError("ASB request failed") from exc
        try:
            return json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AsbInvalidResponseError from exc
