from ruamel.yaml import YAML
from charset_normalizer import from_path
from typing import Any
import os

class ConfigManager:
    def __init__(self, config_path: str = "config.json5"):
        self.config_path = config_path
        self.yaml = YAML(typ='rt')
        self.yaml.preserve_quotes = True
        self.data = self._read_config_file()

    def _read_config_file(self) -> dict:
        if not os.path.exists(self.config_path):
            return self.yaml.load("{}") 
        
        encoding = from_path(self.config_path).best().encoding
        with open(self.config_path, "r", encoding=encoding) as f:
            data = self.yaml.load(f)
        return data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, new_value: Any) -> None:
        self.data[key] = new_value

    def save(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            self.yaml.dump(self.data, f)
