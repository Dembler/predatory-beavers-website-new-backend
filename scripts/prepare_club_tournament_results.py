"""Normalize team-only rows from the user-supplied sponsor archive and cited results.

The extracted private JSON is an input only. The output contains no player identities,
contacts, audience estimates, or other unrelated sponsor information.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/private-history-work/Турниры-результаты.json"
OUT = ROOT / "docs/results-curated.json"
EMBLEM = "/images/brand/club-emblem.webp"


def event(**values: object) -> dict[str, object]:
    return {
        "discipline": "3x3",
        "team": "men",
        "season": str(values["year"]),
        "event_date": None,
        "date_precision": "year",
        "location": None,
        "team_name": "ВГУ — Хищные бобры",
        "opponent_name": None,
        "club_score": None,
        "opponent_score": None,
        "place": None,
        "status": "finished",
        "source_url": None,
        "emblem_url": EMBLEM,
        "opponent_emblem_url": None,
        "notes": None,
        **values,
    }


def apply_reviewed_sources(results: list[dict[str, object]]) -> None:
    """Preserve archive identities while correcting them from reviewed primary sources."""
    by_id = {str(row["external_id"]): row for row in results}
    assk_url = (
        "https://ssca.ru/news/chto-podgotovil-finalnyj-den-assk-festa-pobedy-i-nagrazhdeniya/"
    )
    by_id["sponsors-xlsx-10-men-2024"].update(
        tournament="Суперфинал Чемпионата АССК России · высший дивизион",
        season="2024-2025",
        event_date="2024-11-30",
        date_precision="day",
        location="Федеральная территория Сириус",
        source_url=assk_url,
        source_label="АССК России · официальный итог",
        notes="Архив клуба, строка 10. Первое место мужчин в баскетболе 3×3 высшего дивизиона "
        "независимо подтверждено организатором: Хищные бобры, Горняк, ЮФУ. "
        "Финальный день 30 ноября 2024; место уточнено с Адлера на федеральную территорию Сириус.",
    )
    by_id["sponsors-xlsx-6-women-2024"].update(
        tournament="Суперфинал Чемпионата АССК России · высший дивизион",
        location="Федеральная территория Сириус",
        season="2024-2025",
        source_url="https://ssca.ru/news/assk-fest-itogi-vtorogo-dnya/",
        source_label="АССК России · официальный дайджест",
        notes="Архив клуба, строка 6; участие женщин подтверждено групповыми матчами в дайджесте "
        "АССК ФЕСТа 27–30 ноября 2024. Итоговое место не опубликовано и не восстановлено из игр.",
    )
    by_id["sponsors-xlsx-8-men-2024"].update(
        tournament="Всероссийский студенческий марафон · баскетбол 3×3",
        season="2023-2024",
        source_url="https://t.me/vsumain/2064",
        source_label="ВГУ · официальный канал",
        notes="Архив клуба, строка 8: мужская команда, первое место 2024. Официальный канал ВГУ "
        "отдельно сообщает первое место в турнире по баскетболу 3×3 первого дня ВСМ 2024. "
        "Общее серебро фестиваля не считается отдельной баскетбольной наградой.",
    )
    by_id["sponsors-xlsx-8-men-2025"].update(
        tournament="Всероссийский студенческий марафон · фиджитал-баскетбол",
        discipline="phygital",
        season="2024-2025",
        source_url="https://www.vsu.ru/ru/news/feed/2025/02/18927",
        source_label="ВГУ · новости университета",
        notes="Архив клуба, строка 8: мужская команда, первое место 2025. ВГУ прямо называет "
        "дисциплину фиджитал-баскетболом: прежняя классификация 3×3 исправлена. Фестиваль "
        "проходил 31 января–5 февраля; отдельный день баскетбольного финала не опубликован. "
        "Победы в общем и спортивном зачётах фестиваля не добавляют баскетбольных наград.",
    )
    by_id["sponsors-xlsx-12-men-2023"].update(
        tournament="Кубок ГК «Росатом» «Оранжевый атом» · суперфинал",
        location="Москва · спорткомплекс ЦСКА",
        source_url="https://strana-rosatom.ru/2023/12/28/oranzhevoe-nastroenie-v-moskve-proshel/",
        source_label="Страна Росатом · итог Кубка",
        notes="Архив клуба, строка 12; серебро независимо подтверждено корпоративным изданием "
        "Росатома: в финале Хищные бобры уступили Атомэнергосбыту 15:21. "
        "В мужском турнире участвовали 10 команд. 28 декабря — дата статьи, "
        "не установленный день игры.",
    )
    for team in ("men", "women"):
        by_id[f"sponsors-xlsx-4-{team}-2023"].update(
            source_url="https://www.vsu.ru/ru/news/feed/2023/10/17087",
            source_label="ВГУ · новости университета",
            notes="Архив клуба, строка 4. ВГУ независимо подтверждает: турнир в Иванове "
            "3–7 октября 2023, мужчины — первое место, женщины — третье. "
            "Точный день решающей игры не сообщается; "
            "это отдельный от московского суперфинала этап.",
        )

    universiade = "Межвузовская универсиада Воронежской области"
    for key, year, season, place, team, url, notes in [
        (
            "newspaper-2012-24-universiade-2011-men",
            2011,
            "2011",
            3,
            "men",
            "https://www.vsu.ru/ru/publishing/npvu/docs/vsu_201224.pdf",
            "Газета «Воронежский университет»,25 декабря 2012, № 24(2497), с.3, «БаскетМАНЫ — "
            "супермены!»: третье место мужчин в межвузовской Универсиаде 2011 года. "
            "Точная дата и академический сезон результата не указаны.",
        ),
        (
            "newspaper-2012-24-universiade-2012-men",
            2012,
            "2012-2013",
            2,
            "men",
            "https://www.vsu.ru/ru/publishing/npvu/docs/vsu_201224.pdf",
            "Газета 25 декабря 2012, № 24(2497), с.3: в декабре мужчины заняли второе место, "
            "уступив только ВГИФК. Итоги Универсиады 2012/13 повторно подтверждают серебро "
            "мужского баскетбола в № 14–15(2510–2511),20 июня 2013, с.8: "
            "https://www.vsu.ru/ru/publishing/npvu/docs/vsu_201315.pdf . "
            "Это один турнир, а не отдельные медали 2012 и 2013.",
        ),
        (
            "16219-universiade-2017-men",
            2017,
            "2017",
            1,
            "men",
            "https://www.vsu.ru/ru/news/feed/2023/03/16219",
            "Ретроспектива в новости ВГУ 2023 года: капитан прямо называет 2017 год "
            "предыдущей победой мужчин в Универсиаде. День финала и счёт не опубликованы.",
        ),
        (
            "16219-universiade-2023-men",
            2023,
            "2022-2023",
            1,
            "men",
            "https://www.vsu.ru/ru/news/feed/2023/03/16219",
            "Универсиада завершилась 29 марта 2023. Из 9 мужских команд ВГУ занял первое место, "
            "ВГАС — второе, ВГТУ — третье. Первая мужская победа после 2017 года.",
        ),
        (
            "16219-universiade-2023-women",
            2023,
            "2022-2023",
            2,
            "women",
            "https://www.vsu.ru/ru/news/feed/2023/03/16219",
            "Универсиада завершилась 29 марта 2023. Из 7 женских команд ВГУ занял второе место, "
            "ВГАС — первое, ВИ МВД — третье.",
        ),
        (
            "20557-universiade-men",
            2026,
            "2025-2026",
            2,
            "men",
            "https://www.vsu.ru/ru/news/feed/2026/07/20557",
            "Итоги межвузовской Универсиады 2025/26, новость 16 июля 2026: мужской баскетбол "
            "в списке шести серебряных наград ВГУ.15 сентября 2025–16 июня 2026 — период "
            "всей Универсиады; день баскетбольного финала не указан.",
        ),
        (
            "20557-universiade-women",
            2026,
            "2025-2026",
            3,
            "women",
            "https://www.vsu.ru/ru/news/feed/2026/07/20557",
            "Итоги межвузовской Универсиады 2025/26, новость 16 июля 2026: женский баскетбол "
            "в списке семи бронзовых наград ВГУ. Дата закрытия 1 июля не объявляется "
            "датой баскетбольного финала; точный день игры не опубликован.",
        ),
    ]:
        values: dict[str, object] = {}
        if key.startswith("16219-universiade-2023"):
            values.update(event_date="2023-03-29", date_precision="day")
        results.append(
            event(
                source="vsu",
                external_id=key,
                year=year,
                season=season,
                team=team,
                discipline="5x5",
                tournament=universiade,
                place=place,
                location="Воронеж",
                source_url=url,
                source_label="ВГУ · университетский архив",
                notes=notes,
                **values,
            )
        )
    results.append(
        event(
            source="vsu",
            external_id="newspaper-2012-24-student-league-2012-men",
            year=2012,
            season="2012",
            discipline="5x5",
            tournament="Студенческая лига · финал",
            place=3,
            source_url="https://www.vsu.ru/ru/publishing/npvu/docs/vsu_201224.pdf",
            source_label="ВГУ · газета «Воронежский университет»",
            notes="№ 24(2497),25 декабря 2012, с.3: статья подводит итоги сезона-2012 "
            "и сообщает третье место мужчин в финале Студенческой лиги. Организатор, место "
            "и день финала не указаны. Не отождествлено с АСБ: участие в АСБ в статье "
            "названо планом на следующий сезон.",
        )
    )
    memorial_url = "https://sport-vrn.ru/news/komanda-vgu-stala-pobeditelem-turnira-po-basketbolu-pamyati-b-m-bogacheva/"
    for key, opponent, scored, conceded, place in [
        ("place", None, None, None, 1),
        ("semifinal", "ВГПУ", 65, 54, None),
        ("final", "ВГАС", 57, 39, None),
    ]:
        results.append(
            event(
                source="regional_sport",
                external_id=f"bogachev-2024-men-{key}",
                year=2024,
                season="2023-2024",
                discipline="5x5",
                tournament="Турнир памяти Б. М. Богачева",
                place=place,
                event_date="2024-01-09",
                date_precision="day",
                location="Воронеж · спортзал ВГУИТ",
                opponent_name=opponent,
                club_score=scored,
                opponent_score=conceded,
                source_url=memorial_url,
                source_label="Городской центр спорта Воронежа · официальный итог",
                notes="Официальный отчёт 9 января 2024 сообщает о завершившемся сегодня турнире. "
                "Участвовали ВГУ, ВГАС, ВГУИТ и ВГПУ; ВГУ победил в полуфинале 65:54 "
                "и финале 57:39. Отдельные победы в матчах не считаются дополнительными титулами.",
            )
        )
    results.append(
        event(
            source="rosatom",
            external_id="orange-atom-2023-superfinal-men-final",
            year=2023,
            tournament="Кубок ГК «Росатом» «Оранжевый атом» · суперфинал",
            location="Москва · спорткомплекс ЦСКА",
            opponent_name="Атомэнергосбыт",
            club_score=15,
            opponent_score=21,
            source_url="https://strana-rosatom.ru/2023/12/28/oranzhevoe-nastroenie-v-moskve-proshel/",
            source_label="Страна Росатом · итог Кубка",
            notes="В финале Атомэнергосбыт победил Хищных бобров 21:15; дата статьи 28 декабря "
            "не используется как день игры. Серебро уже учтено единственной турнирной записью.",
        )
    )
    results.append(
        event(
            source="rfb",
            external_id="russia-cup-2026-men-participation",
            year=2026,
            discipline="phygital",
            status="participated",
            tournament="Кубок России по баскетбольному двоеборью",
            location="Москва · баскетбольный центр Playground",
            source_url="https://russiabasket.ru/post/213535",
            source_label="РФБ · официальный обзор",
            notes="Участие подтверждено четырьмя групповыми матчами 21–22 августа; итоговое место "
            "не опубликовано. Турнир 21–23 августа 2026, сроки: "
            "https://russiabasket.ru/post/213530 . "
            "Счета первых трёх встреч: https://russiabasket.ru/post/213533 . "
            "Медаль или первое место по отдельным матчам не выводятся.",
        )
    )


def build() -> dict[str, object]:
    results = []
    # XLSX rows are retained as provenance; discipline is explicit in the tournament title
    # except traditional 5x5 events; reviewed primary-source overrides below distinguish
    # the 2024 3x3 student marathon from the 2025 phygital event.
    locations = {
        4: "Иваново",
        6: "Краснодар",
        8: "Ольгинка",
        10: "Адлер",
        12: "Москва",
        14: "Москва",
        16: "Воронеж",
        18: "Воронеж",
        20: "Воронеж",
        22: "Воронеж",
        24: "Санкт-Петербург",
        26: "Городец",
        28: "Воронеж",
    }
    women_locations = {
        4: "Иваново",
        6: "Адлер",
        8: "Воронеж",
        10: "Воронеж",
        12: "Ставрополь",
        14: "Воронеж",
        16: "Воронеж",
    }
    for row in json.loads(SOURCE.read_text(encoding="utf-8")):
        number, cells = int(row["row"]), row["values"]
        if number < 4:
            continue
        for team, offset in [("men", 0), ("women", 4)]:
            if len(cells) <= offset + 1:
                continue
            title, placement = cells[offset], cells[offset + 1]
            if number == 6 and team == "men":
                # The source's 2023 conflicts with the university's March 2024 report.
                continue
            title_lower = title.lower()
            discipline = (
                "phygital"
                if "фиджитал" in title_lower
                else "3x3"
                if any(x in title_lower for x in ["3х3", "3x3", "марафон"])
                else "5x5"
            )
            matches = re.findall(r"(20\d{2})\s*г?\s*[-–—]?\s*(\d+)\s*место", placement)
            if not matches:
                year_match = re.search(r"20\d{2}", placement)
                if not year_match:
                    raise ValueError(f"Missing year in source row {number}/{team}")
                matches = [(year_match.group(), None)]
            for year, place in matches:
                results.append(
                    event(
                        source="club_archive",
                        external_id=f"sponsors-xlsx-{number}-{team}-{year}",
                        source_label="Архив клуба · таблица для спонсоров",
                        team=team,
                        year=int(year),
                        tournament=title,
                        discipline=discipline,
                        place=int(place) if place else None,
                        status="finished" if place else "participated",
                        location=(locations if team == "men" else women_locations).get(number),
                        notes=f"Таблица из архива «Для спонсоров», строка {number}. "
                        "В источнике указан только год; дата и счёт не сообщаются."
                        + (
                            " Формат 3×3 подтверждён сообщением ВГУ о ВСМ-2024."
                            if "марафон" in title_lower
                            else ""
                        ),
                    )
                )
    results.extend(
        [
            event(
                source="vsu",
                external_id="17849-regions",
                year=2024,
                season="2023-2024",
                tournament="АСБ · Лига Белова · Кубок регионов",
                discipline="5x5",
                place=1,
                event_date="2024-03-29",
                date_precision="day",
                location="Краснодар",
                source_url="https://www.vsu.ru/ru/news/feed/2024/04/17849",
                source_label="ВГУ · новости университета",
                notes="Финал по протоколу АСБ 819230 — 29 марта 2024: ВГУ 98:64 СевГУ. "
                "Новость ВГУ указывает даты турнира 29–30 марта. В спонсорской таблице указан "
                "2023; год исправлен на 2024 по официальным источникам.",
            ),
            event(
                source="asb_3x3",
                external_id="voronezh-2026-men",
                year=2026,
                tournament="АСБ 3×3 · региональный этап",
                place=3,
                event_date="2026-06-27",
                date_precision="day",
                location="Воронеж · ЦУБ «Алые Паруса», ул. Арзамасская, 4Д",
                source_url="https://3x3.asbasket.ru/competitions",
                source_label="АСБ 3×3 · официальные итоги",
                notes="Мужской пьедестал: ВГАС, NOCTA ВГТУ, ВГУ. "
                "Женская ВГУ в опубликованной тройке не указана.",
            ),
            event(
                source="vsu",
                external_id="20726-men",
                year=2026,
                place=3,
                tournament="Кубок «Оранжевый атом» среди опорных вузов Росатома",
                location="Смоленск",
                event_date="2026-10-03",
                date_precision="day",
                source_url="https://www.vsu.ru/ru/news/feed/2026/10/20726",
                source_label="ВГУ · новости университета",
                notes="2–3 октября. Мужской пьедестал: МГУ, НИЯУ МИФИ, ВГУ.",
            ),
            event(
                source="vsu",
                external_id="20726-women",
                year=2026,
                place=4,
                team="women",
                tournament="Кубок «Оранжевый атом» среди опорных вузов Росатома",
                location="Смоленск",
                event_date="2026-10-03",
                date_precision="day",
                source_url="https://www.vsu.ru/ru/news/feed/2026/10/20726",
                source_label="ВГУ · новости университета",
                notes="2–3 октября. Перед ВГУ финишировали РХТУ, ЮУрГУ, ЦССТ-ИГЭУ.",
            ),
            event(
                source="vsu",
                external_id="20624-3x3",
                year=2026,
                place=4,
                tournament="Международный фестиваль «Смоленская крепость»",
                location="Смоленск",
                event_date="2026-08-31",
                date_precision="day",
                source_url="https://www.vsu.ru/ru/news/feed/2026/09/20624",
                source_label="ВГУ · новости университета",
                notes="29–31 августа. Команда провела четыре встречи в баскетболе 3×3.",
            ),
            event(
                source="vsu",
                external_id="20624-phygital",
                year=2026,
                place=3,
                discipline="phygital",
                tournament="Международный фестиваль «Смоленская крепость»",
                location="Смоленск",
                event_date="2026-08-31",
                date_precision="day",
                source_url="https://www.vsu.ru/ru/news/feed/2026/09/20624",
                source_label="ВГУ · новости университета",
                notes="29–31 августа. Третье место в баскетбольном двоеборье; четыре встречи.",
            ),
            event(
                source="atom_sport",
                external_id="1295-men-place",
                year=2025,
                place=2,
                tournament="Кубок ГК «Росатом» «Оранжевый атом» · отборочный тур",
                location="Иваново · ДИВС",
                source_url="https://atom-sport.org/press/news/1295/",
                source_label="Атом-спорт · официальный итог",
                notes="Публикация 6 октября 2025. Финал МГУ — Хищные бобры 20:14. "
                "День матча в публикации не указан.",
            ),
            event(
                source="atom_sport",
                external_id="1295-men-final",
                year=2025,
                tournament="Кубок ГК «Росатом» «Оранжевый атом» · финал отборочного тура",
                location="Иваново · ДИВС",
                opponent_name="МГУ",
                club_score=14,
                opponent_score=20,
                source_url="https://atom-sport.org/press/news/1295/",
                source_label="Атом-спорт · официальный итог",
                notes="Финальный матч. Публикация 6 октября 2025; точный день игры не сообщается.",
            ),
            event(
                source="atom_sport",
                external_id="1070-women",
                year=2023,
                place=1,
                team="women",
                tournament="Летняя студенческая «Атомиада-2023»",
                location="Нижегородская область",
                source_url="https://atom-sport.org/press/news/1070/",
                source_label="Атом-спорт · официальный итог",
                notes="В женском баскетболе 3×3 ВГУ занял первое место, "
                "ИГЭУ — второе, РХТУ — третье.",
            ),
        ]
    )
    for team, opponent, scored, conceded, key in [
        ("men", "Кронверкские барсы", 9, 4, "men-bars"),
        ("women", "ЮФУ", 2, 5, "women-yufu"),
        ("women", "Чёрные Медведи — Политех", 3, 5, "women-polytech"),
    ]:
        results.append(
            event(
                source="assk",
                external_id=f"fest-2024-{key}",
                year=2024,
                team=team,
                tournament="Суперфинал Чемпионата АССК России · высший дивизион · группа Б",
                location="Федеральная территория Сириус",
                opponent_name=opponent,
                club_score=scored,
                opponent_score=conceded,
                source_url="https://ssca.ru/news/assk-fest-itogi-vtorogo-dnya/",
                source_label="АССК России · официальный дайджест",
                notes="АССК ФЕСТ 27–30 ноября 2024. Счёт указан в баскетбольном разделе дайджеста "
                "29 ноября; отдельная дата матча не приведена.",
            )
        )
    for opponent, scored, conceded, day, article, key in [
        ("ФБХК", 22, 46, 21, "213533", "fbhk"),
        ("Купол-Родники", 22, 38, 21, "213533", "kupol-rodniki"),
        ("Takeshot White", 25, 39, 21, "213533", "takeshot-white"),
        ("Автодор", 27, 35, 22, "213535", "avtodor"),
    ]:
        results.append(
            event(
                source="rfb",
                external_id=f"russia-cup-2026-{key}",
                discipline="phygital",
                year=2026,
                tournament="Кубок России по баскетбольному двоеборью · групповой этап",
                event_date=f"2026-08-{day}",
                date_precision="day",
                location="Москва · баскетбольный центр Playground",
                team_name="Хищные бобры",
                opponent_name=opponent,
                club_score=scored,
                opponent_score=conceded,
                source_url=f"https://russiabasket.ru/post/{article}",
                source_label="РФБ · официальный обзор",
                notes="Мужской Кубок России проходил 21–23 августа 2026 года. "
                "Дата игры установлена по указанному в обзоре игровому дню; "
                "публикация вышла на следующий день. После группы команда выбыла; "
                "итоговое место не опубликовано. Сроки и площадка: "
                "https://russiabasket.ru/post/213530",
            )
        )
    apply_reviewed_sources(results)
    return {
        "version": 1,
        "results": results,
        "coverage": [
            {
                "provider": "fiba3x3",
                "team": team,
                "season": "all",
                "records": 0,
                "status": "upstream_unavailable",
                "source_url": "https://play.fiba3x3.com/",
                "note": "Публичный сайт FIBA 3x3 отвечает HTTP 403 (CloudFront Request blocked). "
                "Результаты FIBA не импортированы; опубликованные итоги 3×3 взяты у АСБ, "
                "АССК, ВГУ, Атом-спорт и из архива клуба.",
            }
            for team in ["men", "women"]
        ],
    }


if __name__ == "__main__":
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(payload['results'])} public team results")
