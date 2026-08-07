import argparse
import asyncio
from getpass import getpass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import get_settings


async def create_admin(username: str, email: str) -> None:
    password = getpass("Admin password: ")
    confirmation = getpass("Repeat password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters")

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = User(
                username=username.strip().lower(),
                email=email.strip().lower(),
                password_hash=PasswordSecurity().hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise SystemExit("A user with this username or email already exists") from exc
    finally:
        await engine.dispose()
    print(f"Admin '{username}' created")


async def seed_club() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    teams = (
        ("men", "Хищные Бобры — мужская команда", TeamCategory.MEN),
        ("women", "Хищные Бобры — женская команда", TeamCategory.WOMEN),
    )
    try:
        async with session_factory() as session:
            for slug, name, category in teams:
                exists = await session.scalar(select(func.count(Team.id)).where(Team.slug == slug))
                if not exists:
                    session.add(Team(slug=slug, name=name, category=category, active=True))
            await session.commit()
    finally:
        await engine.dispose()
    print("Club teams seeded")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Predatory Beavers maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    admin_parser = subparsers.add_parser("create-admin", help="Create a local admin user")
    admin_parser.add_argument("--username", required=True)
    admin_parser.add_argument("--email", required=True)
    subparsers.add_parser("seed-club", help="Create the men's and women's teams")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "create-admin":
        asyncio.run(create_admin(args.username, args.email))
    elif args.command == "seed-club":
        asyncio.run(seed_club())


if __name__ == "__main__":
    main()
