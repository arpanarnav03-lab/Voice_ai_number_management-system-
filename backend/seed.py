"""Add numbers to the pool from a CSV you supply (one column named phone_number).

    python seed.py my_numbers.csv

Each number is checked against Bolna (the simulated Bolna in fake mode) exactly like the
'Add to pool' button does. No numbers are shipped in the repo; numbers.example.csv holds
obviously fake ones for the default fake mode."""
import argparse
import asyncio
import csv
import sys

from app import services
from app.bolna_client import RealBolnaClient
from app.config import Settings
from app.db import Database
from app.errors import AppError
from app.fake_bolna import SimulatedBolna


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    args = ap.parse_args()

    settings = Settings.from_env()
    db = Database(settings.database_url)
    db.migrate()
    bolna = RealBolnaClient(settings.bolna_api_key) if settings.bolna_mode == "real" else SimulatedBolna()

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "phone_number" not in reader.fieldnames:
            sys.exit("The CSV needs a header row with a column named phone_number")
        phones = [(row.get("phone_number") or "").strip() for row in reader]

    async def fetch():
        try:
            return await bolna.list_phone_numbers()
        finally:
            await bolna.aclose()

    bolna_numbers = asyncio.run(fetch())
    added = skipped = 0
    for phone in filter(None, phones):
        try:
            with db.session() as s:
                _, warning = services.add_number(s, phone, bolna_numbers)
            added += 1
            print(f"added    {phone}" + (f"  (warning: linked to an agent in Bolna)" if warning else ""))
        except AppError as e:
            skipped += 1
            print(f"skipped  {phone}: {e.message}")
    print(f"\nDone: {added} added, {skipped} skipped.")
    db.dispose()


if __name__ == "__main__":
    main()
