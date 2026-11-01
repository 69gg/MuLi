"""Enabled bot connections name their real private-chat owners."""

from __future__ import annotations

from pathlib import Path

import pytest

from muli.channels import ChannelError, ChannelProfile, ChannelsConfig
from muli.config import ConfigFile, Settings


@pytest.mark.parametrize("owners", [[], [""], ["nickname"], [" 123"], ["0"], ["0123"], ["１２３"]])
def test_onebot_rejects_missing_or_invalid_owner_before_configuration_is_saved(
    tmp_path: Path, owners: list[str]
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"persona": {"user_name": "用户"}})
    before = config.path.read_bytes()
    with pytest.raises(ValueError, match="主人 QQ 号"):
        config.update({"channels": {"onebot": {"enabled": True, "owner_ids": owners}}})
    assert config.path.read_bytes() == before
    assert config.settings.channels.onebot.enabled is False


def test_owner_is_required_for_activation_but_not_for_a_disabled_connection(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    assert not Settings().channels.onebot.enabled
    config.update({"channels": {"onebot": {"enabled": True, "owner_ids": ["12345678"]}}})
    before = config.path.read_bytes()
    with pytest.raises(ValueError, match="主人 QQ 号"):
        config.update({"channels": {"onebot": {"owner_ids": []}}})
    assert config.path.read_bytes() == before
    config.update({"channels": {"onebot": {"enabled": False, "owner_ids": []}}})
    reopened = ConfigFile(config.path)
    assert not reopened.settings.channels.onebot.enabled
    assert reopened.settings.channels.onebot.owner_ids == []
    with pytest.raises(ValueError, match="主人 QQ 号"):
        reopened.update({"channels": {"onebot": {"enabled": True}}})


def test_enabled_weixin_without_an_owner_is_disabled_with_a_warning(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"channels": {"weixin": {"enabled": True}}})
    assert config.settings.channels.weixin.enabled is False
    assert config.settings.channels.owner_warnings == [
        "微信渠道因缺少主人 ID（owner_ids）已停用；请重新绑定主人后再启用"
    ]
    reopened = ConfigFile(config.path)
    assert reopened.settings.channels.weixin.enabled is False


def test_direct_channel_manager_configuration_has_the_same_owner_requirement() -> None:
    with pytest.raises(ChannelError, match="主人 QQ 号"):
        ChannelsConfig(onebot=ChannelProfile(enabled=True))
    assert ChannelsConfig(onebot=ChannelProfile(enabled=True, owner_ids=["12345678"]))
    for name in ("telegram", "qq"):
        with pytest.raises(ChannelError, match="主人 ID"):
            ChannelsConfig(**{name: ChannelProfile(enabled=True)})
        assert ChannelsConfig(
            **{name: ChannelProfile(enabled=True, owner_ids=["platform-user:opaque"])}
        )


@pytest.mark.parametrize("name", ["telegram", "qq"])
def test_other_bot_owners_are_required_without_imposing_the_onebot_number_format(
    tmp_path: Path, name: str
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"persona": {"user_name": "用户"}})
    before = config.path.read_bytes()
    with pytest.raises(ValueError, match="主人 ID"):
        config.update({"channels": {name: {"enabled": True}}})
    assert config.path.read_bytes() == before
    owner = "platform-user:opaque"
    config.update({"channels": {name: {"enabled": True, "owner_ids": [owner]}}})
    before = config.path.read_bytes()
    with pytest.raises(ValueError, match="主人 ID"):
        config.update({"channels": {name: {"owner_ids": []}}})
    assert config.path.read_bytes() == before
    assert getattr(config.settings.channels, name).owner_ids == [owner]
    config.update({"channels": {name: {"enabled": False, "owner_ids": []}}})
    reopened = ConfigFile(config.path)
    assert not getattr(reopened.settings.channels, name).enabled
    assert getattr(reopened.settings.channels, name).owner_ids == []
    with pytest.raises(ValueError, match="主人 ID"):
        reopened.update({"channels": {name: {"enabled": True}}})
