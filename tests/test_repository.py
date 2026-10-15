import json

from app.models import Setting
from app.repository import DEFAULT_VALUES, SECRET_KEYS, _flatten, _repair_secret_row
from app.security import decrypt


def test_yaml_provider_secret_never_replaces_secret_default() -> None:
    flattened = _flatten({"provider": {"api_key": "yaml-secret", "responses_model": "yaml-model"}})
    defaults = dict(DEFAULT_VALUES)
    for key, value in flattened.items():
        if key not in SECRET_KEYS:
            defaults[key] = (value, False)

    assert flattened["provider.api_key"] == "yaml-secret"
    assert defaults["provider.api_key"] == ("", True)
    assert defaults["provider.responses_model"] == ("yaml-model", False)


def test_existing_plaintext_secret_row_is_repaired_and_decryptable() -> None:
    row = Setting(key="provider.api_key", value=json.dumps("synthetic-key"), is_secret=False)
    _repair_secret_row(row)

    assert row.is_secret is True
    assert row.value != json.dumps("synthetic-key")
    assert decrypt(row.value) == "synthetic-key"
