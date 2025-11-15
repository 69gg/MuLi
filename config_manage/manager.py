from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from charset_normalizer import from_path
from typing import Any
import os

class ConfigManager:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.yaml = YAML(typ='rt')
        self.yaml.preserve_quotes = True
        self.data = self._read_config_file()

    def _read_config_file(self) -> CommentedMap:
        if not os.path.exists(self.config_path):
            return self.yaml.load("{}") 
        
        encoding = from_path(self.config_path).best().encoding
        with open(self.config_path, "r", encoding=encoding) as f:
            data = self.yaml.load(f)
        return data

    def get(self, key_path: str, default: Any = None) -> Any:
        keys = key_path.split('.')
        value = self.data
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, key_path: str, new_value: Any) -> None:
        keys = key_path.split('.')
        obj = self.data

        for key in keys[:-1]:
            obj = obj.setdefault(key, CommentedMap())
        
        obj[keys[-1]] = new_value

    def save(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            self.yaml.dump(self.data, f)
