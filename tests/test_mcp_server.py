from mcp.client import Client

from mailpulse.mcp_server.backend import DemoMailbox
from mailpulse.mcp_server.server import create_server


def connect(mailbox: DemoMailbox | None = None) -> Client:
    return Client(create_server(mailbox or DemoMailbox()))


async def test_exposes_tools_with_safety_annotations():
    async with connect() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {
        "search_emails",
        "get_thread",
        "get_attachment",
        "list_pending",
        "create_draft",
        "send_draft",
    }
    assert tools["search_emails"].annotations.read_only_hint is True
    assert tools["create_draft"].annotations.destructive_hint is False
    assert tools["send_draft"].annotations.destructive_hint is True


async def test_search_matches_attachment_text():
    async with connect() as client:
        result = await client.call_tool("search_emails", {"query": "245 000"})

    assert not result.is_error
    assert [hit["message_id"] for hit in result.structured_content["result"]] == [101]


async def test_search_filters_by_importance():
    async with connect() as client:
        result = await client.call_tool("search_emails", {"query": "", "min_importance": 2})

    assert {hit["message_id"] for hit in result.structured_content["result"]} == {101, 102}


async def test_thread_is_ordered_and_marks_outgoing():
    async with connect() as client:
        result = await client.call_tool("get_thread", {"message_id": 102})

    messages = result.structured_content["messages"]
    assert [m["message_id"] for m in messages] == [99, 102]
    assert [m["outgoing"] for m in messages] == [True, False]


async def test_unknown_message_is_a_tool_error():
    async with connect() as client:
        result = await client.call_tool("get_thread", {"message_id": 777})

    assert result.is_error


async def test_pending_lists_reply_and_deadline():
    async with connect() as client:
        result = await client.call_tool("list_pending", {})

    reasons = {item["message_id"]: item["reason"] for item in result.structured_content["result"]}
    assert reasons == {101: "срок 15.09", 102: "ждёт ответа"}


async def test_draft_cannot_be_sent_without_approval():
    mailbox = DemoMailbox()
    async with connect(mailbox) as client:
        created = await client.call_tool("create_draft", {"message_id": 102, "body": "Да, удобно."})
        draft_id = created.structured_content["draft_id"]

        refused = await client.call_tool("send_draft", {"draft_id": draft_id})
        assert refused.is_error

        await mailbox.approve_draft(draft_id)
        sent = await client.call_tool("send_draft", {"draft_id": draft_id})

    assert not sent.is_error
    assert sent.structured_content["status"] == "sent"


async def test_rules_resource():
    async with connect() as client:
        result = await client.read_resource("mailpulse://rules")

    assert "VIP" in result.contents[0].text
