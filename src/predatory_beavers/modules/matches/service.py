"""Backward-compatible service imports; implementations are split by aggregate."""

from predatory_beavers.modules.matches.services.competition import CompetitionService
from predatory_beavers.modules.matches.services.match import MatchService
from predatory_beavers.modules.matches.services.venue import VenueService

__all__ = ["CompetitionService", "MatchService", "VenueService"]
