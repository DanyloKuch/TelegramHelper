"""Google Sheets: запис рядків щоденного чек-іну.

gspread синхронний (блокуючий HTTP), тому всі виклики огортаються в asyncio.to_thread,
щоб не блокувати event loop бота."""
import asyncio

import gspread
from google.oauth2.service_account import Credentials

from src.config import settings as app_settings


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_CHECKIN_SHEET = "Щоденний чек-ін"


class SheetsNotConfigured(RuntimeError):
    pass


def _client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        app_settings.google_sheets_credentials_path, scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _append_row_sync(row: list[str]) -> None:
    ws = _client().open_by_key(app_settings.google_sheets_id).worksheet(_CHECKIN_SHEET)
    ws.append_row(row, value_input_option="USER_ENTERED")


async def append_checkin_row(
    *,
    date_str: str,
    project: str,
    task: str,
    result: str,
    time_spent: str,
    comment: str | None,
) -> None:
    if not app_settings.sheets_configured:
        raise SheetsNotConfigured("Google Sheets не налаштовано (GOOGLE_SHEETS_ID / credentials).")
    row = ["", date_str, project, task, result, time_spent, comment or "", "Зроблено"]
    await asyncio.to_thread(_append_row_sync, row)
