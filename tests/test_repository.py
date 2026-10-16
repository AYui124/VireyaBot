import json

from app.models import Setting
from app.repository import _repair_secret_row
from app.security import decrypt


def test_existing_plaintext_secret_row_is_repaired_and_decryptable() -> None:
    row = Setting(key="provider.api_key", value=json.dumps("synthetic-key"), is_secret=False)
    _repair_secret_row(row)

    assert row.is_secret is True
    assert row.value != json.dumps("synthetic-key")
    assert decrypt(row.value) == "synthetic-key"


