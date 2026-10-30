"""Cold helper imports must not load the unrelated system-events runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

import pytest

from muli.process import run_process

_COLD_IMPORT: Final[str] = """
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

source: Path = Path(sys.argv[1]).resolve(strict=True)
module_name: str = sys.argv[2]
assert sys.flags.isolated == 1
assert module_name in {"muli.config", "muli.managed_host"}
# Neither pytest's module cache nor the editable install may supply the target.
assert not any(name == "muli" or name.startswith("muli.") for name in sys.modules)
sys.path.insert(0, str(source))
importlib.import_module(module_name)

files: dict[str, str] = {}
for name in ("muli", "muli.config", module_name):
    loaded: ModuleType | None = sys.modules.get(name)
    assert isinstance(loaded, ModuleType)
    actual: Path = Path(loaded.__file__).resolve(strict=True)
    expected: Path = (
        source / "muli/__init__.py"
        if name == "muli"
        else source.joinpath(*name.split(".")).with_suffix(".py")
    )
    assert actual == expected, f"wrong source for {name}: {actual}"
    files[name] = str(actual)

runtime_files: dict[str, str] = {}
for name in (
    "muli.system_events", "muli.integrations.attention", "muli.attention", "muli.runtime"
):
    runtime: ModuleType | None = sys.modules.get(name)
    if runtime is not None:
        actual_runtime: Path = Path(runtime.__file__).resolve(strict=True)
        assert actual_runtime == source.joinpath(*name.split(".")).with_suffix(".py")
        runtime_files[name] = str(actual_runtime)

# Import only: no helper main, Settings bypass, database, service or network call.
payload: dict[str, object] = {
    "module": module_name,
    "isolated": sys.flags.isolated,
    "files": files,
    "runtime_files": runtime_files,
}
print(json.dumps(payload, sort_keys=True))
"""

_MODEL_IMPORTS: Final[str] = """
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

source: Path = Path(sys.argv[1]).resolve(strict=True)
first: str = sys.argv[2]
assert sys.flags.isolated == 1 and first in {"muli.config", "muli.system_events"}
assert not any(name == "muli" or name.startswith("muli.") for name in sys.modules)
sys.path.insert(0, str(source))
importlib.import_module(first)
# Observe the light boundary before deliberately importing the legacy runtime.
if first == "muli.config":
    assert "muli.system_events" not in sys.modules
configuration: ModuleType = importlib.import_module("muli.config")
legacy: ModuleType = importlib.import_module("muli.system_events")
shared: ModuleType = importlib.import_module("muli.system_event_settings")
files: dict[str, str] = {}
for name in ("muli", "muli.config", "muli.system_events", "muli.system_event_settings"):
    loaded: ModuleType = sys.modules[name]
    actual: Path = Path(loaded.__file__).resolve(strict=True)
    expected: Path = (
        source / "muli/__init__.py"
        if name == "muli"
        else source.joinpath(*name.split(".")).with_suffix(".py")
    )
    assert actual == expected, f"wrong source for {name}: {actual}"
    files[name] = str(actual)
field = configuration.Settings.model_fields["system_events"]
contracts: dict[str, bool] = {
    "same_model": (
        configuration.SystemEventsSettings is legacy.SystemEventsSettings
        is shared.SystemEventsSettings
    ),
    "same_loglevel": legacy.LogLevel is shared.LogLevel,
    "field_annotation": field.annotation is shared.SystemEventsSettings,
    "field_factory": field.default_factory is shared.SystemEventsSettings,
    "default_instance": type(configuration.Settings().system_events) is shared.SystemEventsSettings,
}
print(json.dumps({"first": first, "files": files, "contracts": contracts}, sort_keys=True))
"""


@pytest.mark.parametrize("module_name", ["muli.config", "muli.managed_host"])
async def test_cold_configuration_import_keeps_system_events_runtime_out(
    module_name: str, tmp_path: Path
) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    # The real bounded runner kills/reaps this one owned child on timeout or
    # shared stdout/stderr overflow, inside the root agent's existing guard.
    result = await run_process(
        [sys.executable, "-I", "-B", "-c", _COLD_IMPORT, str(source), module_name],
        cwd=tmp_path,
        timeout=10,
        limit=4096,
        merge_stderr=False,
        strict_output=True,
    )
    assert result["exit_code"] == 0, result["stderr"] or result["output"]
    observed = json.loads(result["output"])
    assert observed["module"] == module_name and observed["isolated"] == 1
    assert observed["files"] == {
        "muli": str(source / "muli/__init__.py"),
        "muli.config": str(source / "muli/config.py"),
        module_name: str(source.joinpath(*module_name.split(".")).with_suffix(".py")),
    }
    # This assertion is about a nonessential import edge, not every Store or
    # failure path. A fresh process must reach the real module before failing.
    assert observed["runtime_files"] == {}, observed


@pytest.mark.parametrize("first", ["muli.config", "muli.system_events"])
async def test_configuration_and_original_public_entry_share_one_settings_model(
    first: str, tmp_path: Path
) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    # Separate cold processes expose either import cycle; no pytest module is
    # borrowed to make the legacy/config-first identity check appear to work.
    result = await run_process(
        [sys.executable, "-I", "-B", "-c", _MODEL_IMPORTS, str(source), first],
        cwd=tmp_path,
        timeout=10,
        limit=4096,
        merge_stderr=False,
        strict_output=True,
    )
    assert result["exit_code"] == 0, result["stderr"] or result["output"]
    assert json.loads(result["output"]) == {
        "first": first,
        "files": {
            "muli": str(source / "muli/__init__.py"),
            **{
                name: str(source.joinpath(*name.split(".")).with_suffix(".py"))
                for name in (
                    "muli.config",
                    "muli.system_events",
                    "muli.system_event_settings",
                )
            },
        },
        "contracts": {
            "same_model": True,
            "same_loglevel": True,
            "field_annotation": True,
            "field_factory": True,
            "default_instance": True,
        },
    }


@pytest.mark.parametrize(
    ("section", "invalid", "location"),
    [
        ("system_events", {"queue_capacity": 0}, ("system_events", "queue_capacity")),
        ("models", {"main": {"max_output_tokens": 1}}, ("models", "main", "max_output_tokens")),
    ],
)
def test_transaction_url_still_validates_configuration_outside_the_server(
    section: str, invalid: dict[str, object], location: tuple[str, ...]
) -> None:
    from pydantic import ValidationError

    from muli.platforms.linux_controller import transaction_url

    configuration: dict[str, object] = {"server": {"host": "127.0.0.1", "port": 8765}}
    assert transaction_url(json.dumps(configuration).encode(), "readiness") == (
        "http://127.0.0.1:8765/api/internal/management/readiness"
    )
    # A valid loopback/server cannot hide an invalid unrelated configuration.
    configuration[section] = invalid
    with pytest.raises(ValidationError) as caught:
        transaction_url(json.dumps(configuration).encode(), "readiness")
    assert any(error["loc"] == location for error in caught.value.errors())
