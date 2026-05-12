"""FeatureFlag: JSON-backed, fail-closed flag reader.

Phase V's only runtime switch. A missing or unreadable flag file resolves
to ``enabled=False`` so a new code path with a missing flag never silently
activates. Never raises -- callers can wrap pipeline branches in
``if FeatureFlag(...).is_enabled(...)`` without try/except clutter.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Union

_LOG = logging.getLogger(__name__)

PHASE_V_FLAG_NAME = "phase_v_post_hoc_verification"
PHASE_W_FLAG_NAME = "phase_w_agenda_detection"


class FeatureFlag:
    """Read fail-closed feature flags from the data lake.

    The lookup path is::

        <data_lake_path>/store/artifacts/config/<flag_name>_enabled.json

    The JSON file must declare an ``enabled`` boolean. Anything else
    (missing file, malformed JSON, missing key) resolves to False.
    """

    def __init__(self, data_lake_path: Union[str, pathlib.Path]):
        self.data_lake_path = pathlib.Path(data_lake_path)

    def _flag_path(self, flag_name: str) -> pathlib.Path:
        return (
            self.data_lake_path
            / "store"
            / "artifacts"
            / "config"
            / f"{flag_name}_enabled.json"
        )

    def is_enabled(self, flag_name: str) -> bool:
        flag_file = self._flag_path(flag_name)
        try:
            data = json.loads(flag_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "feature_flag_unreadable: %s -> defaulting to False. %s",
                flag_name, exc,
            )
            return False
        if not isinstance(data, dict):
            _LOG.warning(
                "feature_flag_malformed: %s -> not a JSON object", flag_name,
            )
            return False
        return bool(data.get("enabled", False))
