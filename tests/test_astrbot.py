"""Required integration checks against an installed, real AstrBot SDK."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File
from astrbot.api.star import Star
from astrbot.core.star.filter.regex import RegexFilter
from astrbot_plugin_jm_download.delivery import DeliveryTarget
from astrbot_plugin_jm_download.main import COMMAND_FILTER, JmDownloadPlugin


class Event:
    def __init__(self, text):
        self.text = text
        self.unified_msg_origin = "qq-test:FriendMessage:openid"
        self.send = AsyncMock()
        self.should_call_llm = Mock()
        self.stop_event = Mock()

    def get_message_str(self):
        return self.text

    def get_platform_name(self):
        return "qq_official"

    def get_platform_id(self):
        return "qq-test"

    def get_sender_id(self):
        return "openid"

    def get_group_id(self):
        return ""

    def plain_result(self, text):
        return MessageChain().message(text)


@pytest.mark.parametrize(
    "text,album",
    [
        ("/jm123456", "123456"),
        ("/JM 00123456", "123456"),
        ("jm123456", "123456"),
        ("JM 00123456", "123456"),
        ("jm", None),
        ("/jm", None),
        ("/jm0", None),
        ("/jm -1", None),
        ("/jm1 2", None),
        ("/jm1;evil", None),
        ("/jm" + "9" * 5000, None),
    ],
)
async def test_real_sdk_plugin_command(text, album):
    plugin = JmDownloadPlugin(SimpleNamespace(), {})
    assert isinstance(plugin, Star)
    service = Mock()
    service.reserve.return_value = (None, "accepted")
    plugin.service = service
    event = Event(text)
    await plugin.on_jm(event)
    event.stop_event.assert_called_once()
    event.should_call_llm.assert_called_once_with(False)
    event.send.assert_awaited_once()
    if album:
        service.reserve.assert_called_once_with(
            album,
            "qq-test:openid",
            DeliveryTarget(event.unified_msg_origin, "qq-test", user_id="openid"),
        )
    else:
        service.reserve.assert_not_called()


def test_actual_regex_filter_does_not_capture_other_commands():
    regex = RegexFilter(COMMAND_FILTER)
    for text in ("/jm1", "/JM 2", "/jm", "/jm-1", "jm1", "JM 2", "jm"):
        assert regex.filter(Event(text), {})
    for text in ("normal text", "/jmusic", "look /jm1", "/jmhelp", "jmhelp"):
        assert not regex.filter(Event(text), {})


async def test_real_file_component_and_original_session(tmp_path):
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official"))
    context = SimpleNamespace(
        send_message=AsyncMock(return_value=True),
        get_platform_inst=Mock(return_value=platform),
    )
    plugin = JmDownloadPlugin(context, {})
    path = tmp_path / "has spaces" / "JM123.zip"
    target = DeliveryTarget(
        "qq-test:GroupMessage:group-openid", "qq-test", "group-openid"
    )
    await plugin._send(target, "done", path)
    origin, chain = context.send_message.call_args.args
    assert origin == "qq-test:GroupMessage:group-openid"
    assert isinstance(chain, MessageChain)
    assert isinstance(chain.chain[1], File)
    assert chain.chain[1].file_ == str(path)
    context.send_message.return_value = False
    with pytest.raises(RuntimeError):
        await plugin._send(target, "done", path)


@pytest.mark.parametrize(
    "raw,album", [("/jm123456", "123456"), ("/jm 123456", "123456"), ("/jm", None)]
)
async def test_real_pipeline_strips_prefix_and_never_requests_llm(
    monkeypatch, raw, album
):
    """Exercise waking, registered filters and dispatch without a provider."""
    from astrbot.api.message_components import Plain
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.process_stage.method.star_request import (
        StarRequestSubStage,
    )
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.session_plugin_manager import SessionPluginManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    class PipelineEvent(AstrMessageEvent):
        async def send(self, message):
            self._has_send_oper = True
            self.sent.append(message)

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.sender = MessageMember(user_id="openid", nickname="Test")
    message.message = [Plain(raw)]
    message.message_str = raw
    message.message_id = "test-message"
    message.raw_message = None
    event = PipelineEvent(
        raw, message, PlatformMetadata("qq_official", "test", "qq-test"), "openid"
    )
    event.sent = []
    plugin = JmDownloadPlugin(SimpleNamespace(), {})
    plugin.service = Mock()
    plugin.service.reserve.return_value = (None, "accepted without LLM")
    handler = next(
        h
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
        if h.handler_module_path == plugin.__module__ and h.handler_name == "on_jm"
    )
    monkeypatch.setattr(handler, "handler", plugin.on_jm)
    monkeypatch.setattr(
        star_map[plugin.__module__], "name", "astrbot_plugin_jm_download"
    )
    monkeypatch.setattr(
        star_handlers_registry, "get_handlers_by_event_type", lambda *a, **k: [handler]
    )

    async def session_filter(event, handlers):
        return handlers

    monkeypatch.setattr(
        SessionPluginManager, "filter_handlers_by_session", session_filter
    )
    config = {
        "platform_settings": {},
        "wake_prefix": ["/"],
        "admins_id": [],
        "provider_settings": {"enable": True, "prompt_prefix": "", "identifier": False},
    }
    context = PipelineContext(config, SimpleNamespace(), "jm-test")
    waking = WakingCheckStage()
    await waking.initialize(context)
    await waking.process(event)
    assert event.message_str == raw[1:]
    assert event.get_extra("activated_handlers") == [handler]
    processing = ProcessStage()
    processing.ctx = context
    processing.star_request_sub_stage = StarRequestSubStage()
    await processing.star_request_sub_stage.initialize(context)
    llm_request = Mock(side_effect=AssertionError("JM must never invoke an LLM"))
    processing.agent_sub_stage = SimpleNamespace(process=llm_request)
    async for _ in processing.process(event):
        pass
    llm_request.assert_not_called()
    assert event.is_stopped() and len(event.sent) == 1
    if album:
        plugin.service.reserve.assert_called_once()
        assert plugin.service.reserve.call_args.args[0] == album
    else:
        plugin.service.reserve.assert_not_called()
