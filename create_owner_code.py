"""Issue a one-time owner activation/renewal code after a manual payment."""

import argparse

from config import PLATFORM_DATABASE_PATH, PLATFORM_DATABASE_URL
from platform_store import PlatformStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Выдать одноразовый код Такт")
    parser.add_argument(
        "--expires-days",
        type=int,
        default=30,
        help="Сколько дней код можно активировать (по умолчанию: 30)",
    )
    parser.add_argument("--plan", help="Тарифный идентификатор, например control или network")
    parser.add_argument("--point-limit", type=int, help="Максимум активных ПВЗ по коду")
    args = parser.parse_args()
    if not 1 <= args.expires_days <= 3650:
        parser.error("--expires-days должен быть от 1 до 3650")
    code = PlatformStore(PLATFORM_DATABASE_URL or PLATFORM_DATABASE_PATH).issue_owner_code(
        args.expires_days, plan=args.plan, point_limit=args.point_limit
    )
    print(code)


if __name__ == "__main__":
    main()
