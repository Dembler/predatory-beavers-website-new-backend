"""Count documented placements by team, discipline, edition and awarded stage."""

import re
from collections import defaultdict
from collections.abc import Iterable

from predatory_beavers.modules.results.models import ArchiveResult

# These are reviewed championship finals, not a substring search for «чемпионат».
# The national highest-division ASSK final is explicitly confirmed by its organizer.
_CHAMPIONSHIP_RESULTS = frozenset({("club_archive", "sponsors-xlsx-10-men-2024")})


def _normalized(value: str) -> str:
    normalized = re.sub(r"3[хx×]3", "3x3", value.casefold().replace("ё", "е"))
    return re.sub(r"\W+", "", normalized)


def _preferred(rows: list[ArchiveResult]) -> ArchiveResult | None:
    # Two incompatible reported places cannot silently become an extra medal.
    if len({row.place for row in rows if row.place is not None}) > 1:
        return None
    return max(
        rows,
        key=lambda row: (
            row.place is not None,
            bool(row.source_url),
            row.event_date is not None,
            row.source or "",
            row.external_id or "",
            str(row.id),
        ),
    )


def unique_tournament_results(results: Iterable[ArchiveResult]) -> list[ArchiveResult]:
    """Return one supported summary per awarded event, including non-podium places.

    The full stage/division name is retained in the key. An explicit academic season
    identifies one edition across calendar years (e.g. Dec 2012 and a June 2013
    season report). Distinct known event days remain distinct editions. An undated
    citation is merged only when there is at most one dated candidate. A conflicting
    placement is withheld until reviewed; match scores never become placements.
    """
    groups: dict[tuple[str, str, str, str, str], list[ArchiveResult]] = defaultdict(list)
    for row in results:
        if row.opponent_name is not None or row.status not in {"finished", "participated"}:
            continue
        edition = row.season if re.fullmatch(r"20\d{2}-20\d{2}", row.season) else str(row.year)
        # Venue detail is provenance; the city identifies the tournament location.
        city = (row.location or "").split("·", 1)[0]
        key = (row.team, row.discipline, edition, _normalized(row.tournament), _normalized(city))
        groups[key].append(row)
    selected: list[ArchiveResult] = []
    for rows in groups.values():
        dates = {row.event_date for row in rows if row.event_date is not None}
        candidates = (
            [[row for row in rows if row.event_date == day] for day in sorted(dates)]
            if len(dates) > 1
            else [rows]
        )
        for group in candidates:
            preferred = _preferred(group)
            if preferred is not None:
                selected.append(preferred)
    return sorted(
        selected,
        key=lambda row: (
            -row.year,
            row.place or 999,
            row.tournament,
            row.team,
            row.external_id or "",
        ),
    )


def honours_summary(results: Iterable[ArchiveResult]) -> dict[str, object]:
    placements = [
        row
        for row in unique_tournament_results(results)
        if row.status == "finished" and row.place in (1, 2, 3)
    ]
    wins = [row for row in placements if row.place == 1]
    return {
        "titles": len(wins),
        "championships": sum(
            (row.source, row.external_id) in _CHAMPIONSHIP_RESULTS for row in wins
        ),
        "silver": sum(row.place == 2 for row in placements),
        "bronze": sum(row.place == 3 for row in placements),
        "podiums": len(placements),
        "from_year": min((row.year for row in placements), default=None),
        "to_year": max((row.year for row in placements), default=None),
        "methodology": (
            "Документированные итоговые места мужской и женской команд ВГУ в баскетболе 5×5, "
            "3×3 и фиджитал-баскетболе. Команды, дисциплины, сезоны, дивизионы и отдельно "
            "награждаемые этапы учитываются раздельно; повторные сообщения одного итога — "
            "один раз. Победа в матче, общеспортивный рейтинг и общий зачёт фестиваля "
            "не являются баскетбольной наградой. «Титулы» — первые места, включая кубки "
            "и отборочные этапы; «чемпионства» — только отдельно проверенные финалы "
            "чемпионатов. Это подтверждённый минимум доступных источников, а не полная "
            "статистика за всю историю клуба."
        ),
        "items": [
            {
                "id": str(row.id),
                "team": row.team,
                "discipline": row.discipline,
                "season": row.season,
                "year": row.year,
                "tournament": row.tournament,
                "location": row.location,
                "place": row.place,
                "source_label": row.source_label,
                "source_url": row.source_url,
            }
            for row in placements
        ],
    }
