"""Discover older VGU identities from the public ASB competition tree and standings."""

import json
from pathlib import Path

import httpx

BASE = "https://asb.infobasket.su"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/results-research"


def walk(nodes, path=()):
    for node in nodes:
        label = node.get("CompShortNameRu", "")
        here = (*path, label)
        if any(token in label.lower() for token in ["воронеж", "черноз", "центральн"]):
            yield node, here
        yield from walk(node.get("Children", []), here)


if __name__ == "__main__":
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        seasons = client.get(f"{BASE}/Comp/GetSeasonsForTag/?tag=asbRoot").json()
        discoveries = []
        coverage = []
        for season in seasons:
            if season["SeasonYear"] > 2019:
                continue
            response = client.get(f"{BASE}/Widget/CompIssue/{season['CompID']}")
            response.raise_for_status()
            tree = response.json()
            nodes = list(walk(tree.get("Comps", [])))
            print(season["SeasonYear"], [(n["CompID"], "/".join(p)) for n, p in nodes], flush=True)
            for node, path in nodes:
                relevant = [node, *node.get("Children", [])]
                for comp in relevant:
                    url = f"{BASE}/Widget/CompTeamResults/{comp['CompID']}?format=json"
                    rows = client.get(url).json()
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        team_name = row.get("CompTeamName", {})
                        name = team_name.get("CompTeamNameRu", "")
                        region = team_name.get("CompTeamRegionNameRu", "")
                        if (
                            name.strip() == "ВГУ"
                            and ("Воронеж" in "/".join(path) or "Воронеж" in region)
                        ) or (
                            "воронеж" in name.lower()
                            and "универ" in name.lower()
                            and "техн" not in name.lower()
                            and "аграр" not in name.lower()
                        ):
                            selected = {key: row.get(key) for key in ["CompID", "TeamID", "Place"]}
                            selected.update(
                                name=name,
                                region=region,
                                year=season["SeasonYear"],
                                root_comp_id=season["CompID"],
                                path=list(path),
                            )
                            discoveries.append(selected)
                            print("VGU", json.dumps(selected, ensure_ascii=False), flush=True)
            if season["SeasonYear"] <= 2018:
                for team in ["men", "women"]:
                    coverage.append(
                        {
                            "provider": "asb",
                            "team": team,
                            "season": f"{season['SeasonYear'] - 1}-{season['SeasonYear']}",
                            "records": 0,
                            "status": "historical_identity_unverified",
                            "source_url": f"{BASE}/Widget/CompIssue/{season['CompID']}",
                            "note": "Проверено официальное дерево сезона и таблицы найденных веток "
                            "Воронеж/Черноземье. Публичные таблицы не вернули строк ВГУ; "
                            "более ранний идентификатор команды не подтверждён. "
                            "Это пробел доступного источника, "
                            "а не утверждение о неучастии клуба.",
                        }
                    )
        coverage.append(
            {
                "provider": "asb",
                "team": "women",
                "season": "2018-2019",
                "records": 0,
                "status": "historical_identity_unverified",
                "source_url": f"{BASE}/Widget/GetTeamSeasons/7695",
                "note": "Сезон 2018/19 отсутствует в официальном указателе женской команды 7695. "
                "Более ранний идентификатор не подтверждён. Это пробел источника, "
                "а не утверждение о неучастии клуба.",
            }
        )
        (OUT / "legacy-asb-discoveries.json").write_text(
            json.dumps(discoveries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ROOT / "docs/results-asb-legacy-coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
