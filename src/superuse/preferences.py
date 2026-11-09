from __future__ import annotations

import json
import os
from pathlib import Path

from superuse.config import Config, load_config


class Preferences:
    def __init__(self, path: Path):
        self.path = path
        self.values: dict = {}

    def load(self, config: Config) -> Config:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(values, dict) or set(values) - {"power", "device"}:
                raise ValueError("Invalid preferences")
            if any(not isinstance(value, dict) for value in values.values()):
                raise ValueError("Invalid preferences table")
            if "mode" in values.get("device", {}):
                raise ValueError("Device mode is not a user preference")
            merged = load_config(overrides=config.raw).raw
            for key, value in values.items():
                if key == "device":
                    for name, part in value.items():
                        if isinstance(part, dict) and isinstance(merged[key].get(name), dict):
                            merged[key][name].update(part)
                        else:
                            merged[key][name] = part
                else:
                    merged[key] = value
            result = load_config(overrides=merged)
            self.values = values
            return result
        except FileNotFoundError:
            return config

    def save(self, values: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        from superuse.windows.security import apply_user_only_acl

        apply_user_only_acl(temp)
        os.replace(temp, self.path)
        self.values = values
