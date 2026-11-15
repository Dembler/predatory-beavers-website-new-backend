"""Rebuild reviewed official 3x3/phygital manifests without touching the database.

Only source-confirmed facts are recorded. Publication dates and the end of a
multi-day event are never substituted for an unknown game date.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CLUB = "ВГУ — Хищные бобры"
VSU = "https://www.vsu.ru/ru/news/feed/"
FIBA = (
    "https://play.fiba3x3.com/categories/234bd9c7-0e53-4399-b4dd-48674a2718c7/"
    "teams/1054cee5-eb42-4b5b-b73d-da6762dc9e70"
)


def result(
    source: str,
    external_id: str,
    year: int,
    tournament: str,
    url: str,
    *,
    team: str = "men",
    discipline: str = "3x3",
    season: str | None = None,
    event_date: str | None = None,
    location: str | None = None,
    place: int | None = None,
    opponent: str | None = None,
    scores: tuple[int, int] | None = None,
    team_name: str = CLUB,
    label: str = "ВГУ · новости университета",
    notes: str,
) -> dict:
    return {
        "source": source,
        "external_id": external_id,
        "discipline": discipline,
        "team": team,
        "season": season or str(year),
        "year": year,
        "tournament": tournament,
        "event_date": event_date,
        "date_precision": "day" if event_date else "year",
        "location": location,
        "team_name": team_name,
        "opponent_name": opponent,
        "club_score": scores[0] if scores else None,
        "opponent_score": scores[1] if scores else None,
        "place": place,
        "status": "finished",
        "source_url": url,
        "source_label": label,
        "emblem_url": "/images/brand/club-emblem.webp",
        "opponent_emblem_url": None,
        "notes": notes,
    }


def save(name: str, records: list[dict], coverage: list[dict] | None = None) -> None:
    value = {"version": 1, "results": records, "coverage": coverage or []}
    (DOCS / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    records = [
        result(
            "vsu",
            "11620-3x3-men",
            2019,
            "Открытый фестиваль баскетбола 3х3 «Властелины колец»",
            VSU + "2019/10/11620",
            season="2019-2020",
            location="Волгоград · ВолгГТУ",
            place=1,
            notes=(
                "ВГУ сообщает о первом месте команды с капитаном Даниилом Фёдоровым "
                "и Александром Сдобниковым. Фестиваль проходил 17–19 октября 2019 года; "
                "точный день решающего матча и счета не опубликованы."
            ),
        ),
        result(
            "vsu",
            "14994-assk-women",
            2022,
            "Всероссийский суперфинал Чемпионата АССК России · Высший дивизион",
            VSU + "2022/05/14994",
            team="women",
            season="2021-2022",
            location="Казань",
            place=3,
            notes=(
                "Бронза женской команды 3х3 подтверждена ВГУ и организатором АССК: "
                "https://ssca.ru/news/2553/. АССК.ФЕСТ проходил 19–23 мая 2022 года. "
                "Точный день баскетбольного финала и счета не опубликованы; 30 мая "
                "в статье ВГУ — дата встречи делегации с ректором."
            ),
        ),
    ]
    for team in ("men", "women"):
        records.append(
            result(
                "vsu",
                f"15355-asb3x3-{team}",
                2022,
                "АСБ 3х3 · Воронеж",
                VSU + "2022/09/15355",
                team=team,
                season="2022-2023",
                event_date="2022-09-10",
                location="Воронеж · парк «Алые паруса»",
                place=3,
                notes=(
                    "Официальное сообщение ВГУ прямо называет бронзовыми призёрами "
                    "обе команды ССК «Хищные бобры» и приводит их составы. Дата турнира "
                    "10 сентября указана в тексте; счета отдельных матчей не опубликованы."
                ),
            )
        )

    r2d2_identity = (
        "Команда выступала под именем R2D2, а не под названием ВГУ. ВГУ прямо "
        "подтверждает, что весь состав — спортсмены ССК ВГУ «Хищные бобры»: "
        "Евгений Бабкин, Антон Казанин, Никита Янишевский, Леонид Юдин; "
        "https://www.vsu.ru/ru/news/feed/2022/09/15347. "
        "Турнир проходил 2–4 сентября 2022 года. "
    )
    r2d2_tournament = "Первенство России по баскетболу 3х3 U18 · юноши"
    r2d2_location = "Калининград · Центр уличного баскетбола «ПСБ — Детям»"
    records.append(
        result(
            "vsu",
            "15347-r2d2-u18-place",
            2022,
            r2d2_tournament,
            VSU + "2022/09/15347",
            season="2022-2023",
            team_name="R2D2",
            location=r2d2_location,
            place=1,
            notes=r2d2_identity + "Точный день финала в источнике не указан.",
        )
    )
    for stage, opponent, scores in [
        ("group-sverdlovsk", "Свердловская область", (10, 16)),
        ("group-pokemony", "Покемоны", (17, 5)),
        ("quarterfinal-tsop-spb", "ЦОП-Спб", (14, 12)),
        ("semifinal-teen-spirit", "Teen Spirit", (17, 11)),
        ("final-parma-m", "Парма-М", (14, 10)),
    ]:
        records.append(
            result(
                "cspsk_vrn",
                f"r2d2-u18-2022-{stage}",
                2022,
                r2d2_tournament,
                "https://cspsk-vrn.ru/basketbol-3x3-voronezhcy-stali-pobeditelyami-pervenstva-rossii/",
                season="2022-2023",
                team_name="R2D2",
                location=r2d2_location,
                opponent=opponent,
                scores=scores,
                label="ЦСПСК Воронежской области · официальный результат",
                notes=r2d2_identity + "Счет подтвержден ЦСПСК; точный день этой игры не указан.",
            )
        )

    for team, name in [("men", "ВГУ"), ("women", "Хищные бобры")]:
        records.append(
            result(
                "vsu",
                f"17827-assk-intramural-{team}",
                2024,
                "Чемпионат АССК России · внутривузовский этап ВГУ",
                VSU + "2024/03/17827",
                team=team,
                season="2023-2024",
                location="Воронеж · ВГУ",
                team_name=name,
                place=1,
                notes=(
                    "ВГУ публикует победителей баскетбола 3х3 отдельно среди мужчин "
                    "(ВГУ) и девушек («Хищные Бобры»). Это внутривузовский отбор, "
                    "а не всероссийский суперфинал. Игры проходили в феврале–марте; "
                    "29 марта — награждение, точные даты матчей не сообщаются."
                ),
            )
        )
    records.append(
        result(
            "energia",
            "orange-atom-2024-women-quarterfinal-vsu",
            2024,
            "Кубок ГК «Росатом» «Оранжевый атом» · 4-й тур · четвертьфинал",
            "https://energia.russiabasket.ru/news/21087/v-domashnem-ture-s-bronzoj",
            team="women",
            season="2024-2025",
            event_date="2024-10-05",
            location="Иваново · Дворец игровых видов спорта",
            opponent="ЦССТ-ИГЭУ",
            scores=(10, 15),
            label="БК «Энергия» / РФБ · официальный отчёт команды-соперника",
            notes=(
                "Отчет БК «Энергия» прямо описывает победу ЦССТ-ИГЭУ над командой "
                "«Хищные бобры» (ВГУ) в четвертьфинале 5 октября со счетом 15:10. "
                "Счет здесь приведен со стороны ВГУ. Указанное в источнике третье "
                "место ВГУ относится к группе и не записано как итоговое место турнира."
            ),
        )
    )
    records.append(
        result(
            "vsu",
            "19615-orange-atom-women",
            2025,
            "Кубок ГК «Росатом» «Оранжевый атом» · отборочный тур",
            VSU + "2025/10/19615",
            team="women",
            season="2025-2026",
            location="Иваново",
            place=7,
            notes=(
                "ВГУ подтверждает седьмое место женской команды из десяти участниц. "
                "Турнир проходил 3–4 октября 2025 года; точные даты игр и счета "
                "женской команды не опубликованы. Мужское серебро уже учтено в "
                "основном архиве по источнику «Атом-спорт» и здесь не дублируется."
            ),
        )
    )
    records.append(
        result(
            "vsu",
            "20708-sukhumi-men",
            2026,
            "Международный фестиваль баскетбола 3х3 имени Сергея Багапша",
            VSU + "2026/09/20708",
            season="2026-2027",
            location="Сухум",
            team_name="ВГУ — Нововоронежская АЭС",
            place=1,
            notes=(
                "Университет прямо называет победителем сборную ВГУ, представлявшую "
                "Нововоронежскую АЭС: Яков Горностаев, Леонид Юдин, Иван Красников, "
                "Юрий Провоторов. Турнир проходил 26–28 сентября 2026 года; точный "
                "день решающего матча и счета не опубликованы."
            ),
        )
    )
    moscow_tournament = "Кубок ГК «Росатом» «Оранжевый атом» · московский финал"
    moscow_video = "https://vkvideo.ru/video-166448519_456239987"
    moscow_evidence = (
        "Организатор ЦССТ опубликовал пару матча за третье место 6 декабря 2025: "
        "СевГУ «Левиафан» — ВГУ «Хищные бобры», "
        "https://vk.ru/wall-166448519_5328. Затем организатор объявил бронзовым "
        "призером СевГУ: https://vk.ru/wall-166448519_5334. Эти две публикации "
        "подтверждают поражение ВГУ в матче за бронзу и четвертое место. "
        "Общий итог организатора: https://vk.ru/wall-166448519_5336. "
        "Даты 5–6 декабря и Академию спорта «Динамо» независимо подтверждает "
        "https://atom-sport.org/press/news/1314/. Это отдельный от ивановского "
        "отбора 3–4 октября турнир. Итоговая плашка официальной трансляции "
        "https://vkvideo.ru/video-166448519_456239987?t=20880 (5:48:00) "
        "подтверждает счет бронзовой игры: СевГУ 21:15 ВГУ."
    )
    for key, place, opponent, scores in [
        ("place", 4, None, None),
        ("bronze-game-sevgu", None, "СевГУ — Левиафан", (15, 21)),
    ]:
        records.append(
            result(
                "csst",
                f"orange-atom-moscow-2025-men-{key}",
                2025,
                moscow_tournament,
                "https://vk.ru/wall-166448519_5328" if place else moscow_video + "?t=20880",
                season="2025-2026",
                event_date="2025-12-06",
                location="Москва · Академия спорта «Динамо»",
                opponent=opponent,
                scores=scores,
                place=place,
                label="ЦССТ Концерна Росэнергоатом · официальные итоги",
                notes=moscow_evidence,
            )
        )
    records.append(
        result(
            "csst",
            "orange-atom-moscow-2025-men-semifinal-tpu",
            2025,
            moscow_tournament + " · полуфинал",
            moscow_video + "?t=7680",
            season="2025-2026",
            event_date="2025-12-06",
            location="Москва · Академия спорта «Динамо»",
            opponent="ТПУ — Сибирские львы",
            scores=(20, 21),
            label="ЦССТ Концерна Росэнергоатом · официальная трансляция",
            notes=(
                "Итоговая плашка официальной трансляции первого корта 6 декабря 2025 "
                "на отметке 2:08:00 (7680 секунд) прямо показывает ВГУ — Хищные бобры "
                "20:21 ТПУ — Сибирские львы. Это полуфинал; в следующем матче ВГУ "
                "играл с СевГУ за бронзу, ТПУ вышел в финал. Официальное объявление "
                "финальных пар: https://vk.ru/wall-166448519_5328."
            ),
        )
    )
    for key, opponent, scores in [
        ("sevgu", "СевГУ — Левиафан", (21, 7)),
        ("mpei", "НИУ МЭИ", (21, 14)),
        ("mirea", "РТУ МИРЭА", (14, 11)),
    ]:
        records.append(
            result(
                "csst",
                f"orange-atom-moscow-2025-men-group-{key}",
                2025,
                moscow_tournament + " · групповой этап",
                "https://vk.ru/wall-166448519_5316",
                season="2025-2026",
                event_date="2025-12-05",
                location="Москва · Академия спорта «Динамо»",
                opponent=opponent,
                scores=scores,
                label="ЦССТ Концерна Росэнергоатом · официальный протокол дня",
                notes=(
                    "Третье изображение в публикации организатора: «Итоги 1 дня / Юноши», "
                    "финал 5–6 декабря 2025, Москва. На карточке непосредственно "
                    "указаны команды и счет. Первый игровой день — 5 декабря; "
                    "публикация завершает групповой этап словами «До завтра». "
                    "Счет нормализован со стороны ВГУ. Это московский финал, "
                    "а не отборочный тур в Иванове. Постоянная ссылка на карточку: "
                    "https://vk.ru/photo-166448519_457302222."
                ),
            )
        )
    counts = Counter((row["team"], row["season"]) for row in records)
    coverage = [
        {
            "provider": "official_3x3_discovery",
            "team": team,
            "season": season,
            "records": count,
            "status": "partial",
            "source_url": next(
                row["source_url"]
                for row in records
                if (row["team"], row["season"]) == (team, season)
            ),
            "note": "Проверенные дополнительные результаты первичных источников; "
            "это не полный протокол турниров.",
        }
        for (team, season), count in sorted(counts.items())
    ]
    save("results-3x3-official.json", records, coverage)

    phygital = [
        result(
            "vsu",
            "20699-univerliga-phygital",
            2026,
            "УниверЛига · межвузовский Фестиваль инновационных видов спорта",
            VSU + "2026/09/20699",
            discipline="phygital",
            season="2026-2027",
            event_date="2026-09-28",
            location="Фиджитал-центр",
            place=1,
            notes=(
                "ВГУ прямо сообщает дату 28 сентября и первое место именно в "
                "фиджитал-баскетболе. Состав: Илья Кузнецов, Захар Дудкин, Максим "
                "Шибанов; тренер Мария Янишевская. Фестиваль открывал сезон "
                "УниверЛиги 2026–2027. Соперники и счета не опубликованы."
            ),
        )
    ]
    save(
        "results-phygital-official.json",
        phygital,
        [
            {
                "provider": "official_phygital_discovery",
                "team": "men",
                "season": "2026-2027",
                "records": 1,
                "status": "partial",
                "source_url": VSU + "2026/09/20699",
                "note": "Подтвержден итог межвузовского фестиваля 28.09.2026; "
                "протоколы отдельных игр не опубликованы.",
            }
        ],
    )
    save(
        "results-fiba3x3.json",
        [],
        [
            {
                "provider": "fiba3x3",
                "team": team,
                "season": "all",
                "records": 0,
                "status": "upstream_unavailable",
                "source_url": FIBA,
                "note": (
                    "Проверено 11.11.2026: указанная пользователем регистрация и публичный "
                    "сайт play.fiba3x3.com возвращают HTTP 403 CloudFront Request blocked. "
                    "Документированные DivisionGames, DivisionTeams, TeamRegistrationPublic "
                    "и DivisionStanding API возвращают HTTP 403: требуется X-Application-Key. "
                    "Открытый event-api-fiba также требует API key. Пользователь подтвердил "
                    "событие: финал Кубка организаций ГК «Росатом» «Оранжевый атом», "
                    "Москва, 5–6 декабря 2025 года. Организатор независимо подтверждает "
                    "даты и площадку — Академия спорта «Динамо»: "
                    "https://atom-sport.org/press/news/1314/. Это московский финал, "
                    "отдельный от ивановского отбора 3–4 октября. Полный протокол FIBA "
                    "недоступен. Публикации организатора _5328 и _5334 подтверждают "
                    "матч за бронзу ВГУ — СевГУ «Левиафан» 6 декабря, поражение ВГУ "
                    "и четвертое место: https://vk.ru/wall-166448519_5328, "
                    "https://vk.ru/wall-166448519_5334. Они добавлены по источнику ЦССТ "
                    "в results-3x3-official.json. Третья карточка официального поста "
                    "https://vk.ru/wall-166448519_5316 также подтверждает три игры "
                    "группы 5 декабря: ВГУ 21:7 СевГУ, 21:14 НИУ МЭИ, 14:11 РТУ МИРЭА. "
                    "Официальная трансляция https://vkvideo.ru/video-166448519_456239987 "
                    "подтверждает две оставшиеся игры 6 декабря: полуфинал ВГУ 20:21 "
                    "ТПУ «Сибирские львы» на 2:08:00 (7680 секунд), бронза ВГУ 15:21 "
                    "СевГУ на 5:48:00 (20880 секунд). Все пять матчей ВГУ этого финала "
                    "восстановлены по первичным карточкам и трансляции ЦССТ. "
                    "Машиночитаемый протокол FIBA не получен. "
                    "Подтвержденная организатором ссылка на расписание: "
                    "https://play.fiba3x3.com/events/1dac71d9-fa91-448c-b83f-adc9ccbcae65/schedule "
                    "(публичная ссылка организатора vk.cc/cRZVUV). API с этим event ID "
                    "также вернул 403. "
                    "Подтвержденные альтернативные источники находятся в results-3x3-official.json."
                ),
            }
            for team in ("men", "women")
        ],
    )
    game_count = sum(row["club_score"] is not None for row in records)
    print(
        f"Saved {len(records)} 3x3 results ({game_count} games), "
        f"{len(phygital)} phygital result; FIBA gap recorded"
    )


if __name__ == "__main__":
    main()
