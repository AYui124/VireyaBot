"""Exercise the authenticated public API against isolated PostgreSQL schemas."""

import asyncio
import json
import os
import unittest
import uuid

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import Base, ConfigChange, ProviderSecret
from app.providers import ResponseRequest, build_provider
from app.repository import bootstrap, read_provider_runtime_settings


@unittest.skipUnless(os.environ.get("VIREYA_TEST_DATABASE_URL"), "requires isolated PostgreSQL")
class ProviderManagementTest(unittest.TestCase):
    def test_authenticated_lifecycle_and_isolation(self):
        asyncio.run(self.exercise())

    async def exercise_anthropic_paths(self, client, sessions):
        observed = []
        handlers = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                path = raw.split(b" ")[1].decode()
                length = next(int(line.split(b":", 1)[1]) for line in raw.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                payload = json.loads(await reader.readexactly(length))
                observed.append((path, payload["model"]))
                valid = path in {"/v1/messages", "/gateway/v1/messages"}
                message = {"id": "msg-saved-url", "type": "message", "role": "assistant", "model": "saved-model", "content": [{"type": "text", "text": "saved URL works"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 2}}
                if valid and payload.get("stream"):
                    events = [
                        {"type": "message_start", "message": message | {"content": [], "stop_reason": None}},
                        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "saved URL "}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "works"}},
                        {"type": "content_block_stop", "index": 0},
                        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
                        {"type": "message_stop"},
                    ]
                    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(message if valid else {"error": {"type": "not_found_error", "message": "wrong path"}}).encode()
                    content_type = "application/json"
                writer.write(f"HTTP/1.1 {'200 OK' if valid else '404 Not Found'}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        listener = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        try:
            async with listener:
                for prefix in ("", "/gateway"):
                    created = await client.post("/api/providers", json={"name": "Saved Anthropic URL", "protocol_type": "anthropic_messages", "base_url": f"http://127.0.0.1:{port}{prefix}/v1", "timeout_seconds": 3, "responses_model": "saved-model", "api_key": uuid.uuid4().hex})
                    self.assertEqual(created.status_code, 200)
                    provider_id = created.json()["providerId"]
                    for suffix in ("/v1", "/v1/"):
                        url = f"http://127.0.0.1:{port}{prefix}{suffix}"
                        saved = await client.put("/api/providers/" + provider_id, json={"base_url": url})
                        self.assertEqual(saved.status_code, 200)
                        self.assertEqual(saved.json()["providerId"], provider_id)
                        async with sessions() as session:
                            runtime = await read_provider_runtime_settings(session, provider_id)
                            self.assertEqual(runtime["base_url"], url)
                            provider = await build_provider(session, provider_id)
                        try:
                            for streamed in (False, True):
                                result = await provider.responses(ResponseRequest(input="saved URL", stream=streamed), trace_id="saved-url")
                                self.assertEqual(result.output_text, "saved URL works")
                                self.assertEqual(result.id, "msg-saved-url")
                                self.assertEqual(observed[-1], (prefix + "/v1/messages", "saved-model"))
                        finally:
                            await provider._close()
                    self.assertEqual((await client.delete("/api/providers/" + provider_id)).status_code, 200)
        finally:
            if handlers:
                await asyncio.gather(*handlers)

    async def exercise(self):
        url = os.environ["VIREYA_TEST_DATABASE_URL"]
        schema = "test_provider_" + uuid.uuid4().hex
        admin_engine = create_async_engine(url)
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_dependency():
            async with sessions() as session:
                yield session

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as session:
                await bootstrap(session)
            app.dependency_overrides[get_session] = session_dependency
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/api/providers")).status_code, 401)
                self.assertEqual((await client.post("/api/providers", json={})).status_code, 401)
                login = await client.post("/api/login", json={"username": settings.admin_username, "password": settings.admin_password})
                self.assertEqual(login.status_code, 200)
                self.assertEqual((await client.get("/api/providers")).json(), [])
                metadata = (await client.get("/api/provider-protocols")).json()
                ids = []
                for protocol in metadata:
                    payload = {"name": protocol, "protocol_type": protocol, "base_url": "http://127.0.0.1:9/v1", "timeout_seconds": 4, "api_key": "synthetic-key-" + protocol, "responses_model": "synthetic-model", "temperature": None, "default_stream": True, "max_output_tokens": 81, "extra_headers": [{"name": "X-Tenant", "value": "synthetic-header-" + protocol}]}
                    if protocol != "anthropic_messages":
                        payload.update(embedding_model="embedding", embedding_dimensions=2, organization="org", project="proj")
                    response = await client.post("/api/providers", json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
                    saved = response.json()
                    ids.append(saved["providerId"])
                    self.assertEqual(saved["extraHeaderNames"], ["X-Tenant"])
                    self.assertNotIn("synthetic-key", response.text)
                    self.assertNotIn("synthetic-header", response.text)
                    path = "/api/providers/" + saved["providerId"]
                    updated = await client.put(path, json={"name": "renamed-" + protocol, "temperature": metadata[protocol]["temperature_max"]})
                    self.assertEqual(updated.status_code, 200, updated.text)
                    self.assertEqual(updated.json()["providerId"], saved["providerId"])
                    self.assertEqual((await client.put(path, json={"temperature": metadata[protocol]["temperature_max"] + 0.1})).status_code, 422)
                    self.assertEqual((await client.put(path, json={"temperature": None})).status_code, 200)
                    for bad in ({"unknown": "synthetic-secret"}, {"timeout_seconds": 0}, {"max_output_tokens": 0}, {"base_url": "https://user:synthetic-secret@example.test/v1"}, {"base_url": "https://example.test/v1?key=synthetic-secret"}, {"base_url": "https://example.test/v1/responses"}):
                        rejected = await client.put(path, json=bad)
                        self.assertEqual(rejected.status_code, 422)
                        self.assertNotIn("synthetic-secret", rejected.text)
                    if protocol == "anthropic_messages":
                        for field, value in (("embedding_model", "x"), ("organization", ""), ("project", ""), ("native_search_enabled", False)):
                            self.assertEqual((await client.put(path, json={field: value})).status_code, 422)
                    elif protocol == "openai_chat_completions":
                        self.assertEqual((await client.put(path, json={"native_search_enabled": False})).status_code, 422)
                path = "/api/providers/" + ids[0]
                for rows in ([{"name": "Authorization", "value": "synthetic-secret"}], [{"name": "x-api-key", "value": "synthetic-secret"}], [{"name": "OpenAI-Project", "value": "synthetic-secret"}], [{"name": "X-A", "value": "a"}, {"name": "x-a", "value": "b"}], [{"name": "", "value": "a"}], [{"name": "X-A", "value": "synthetic-secret\r\nX-B: x"}]):
                    rejected = await client.put(path, json={"extra_headers": rows})
                    self.assertEqual(rejected.status_code, 422)
                    self.assertNotIn("synthetic-secret", rejected.text)
                renamed = await client.put(path, json={"extra_headers": [{"name": "X-Renamed", "previous_name": "X-Tenant", "value": ""}]})
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual(renamed.json()["extraHeaderNames"], ["X-Renamed"])
                async with sessions() as session:
                    a = await read_provider_runtime_settings(session, ids[0])
                    b = await read_provider_runtime_settings(session, ids[1])
                    self.assertEqual(a["extra_headers"], {"X-Renamed": "synthetic-header-openai_responses"})
                    self.assertEqual(b["extra_headers"], {"X-Tenant": "synthetic-header-openai_chat_completions"})
                    self.assertNotEqual(a["api_key"], b["api_key"])
                self.assertEqual((await client.put(path, json={"api_key": "", "extra_headers": [{"name": "X-Renamed", "value": "replacement"}]})).status_code, 200)
                self.assertEqual((await client.put(path, json={"extra_headers": []})).json()["extraHeaderNames"], [])
                self.assertEqual((await client.put(path, json={"protocol_type": "anthropic_messages"})).status_code, 422)
                unchanged = (await client.get("/api/providers")).json()[0]
                self.assertEqual(unchanged["protocolType"], "openai_responses")
                switched = await client.put(path, json={"protocol_type": "anthropic_messages", "confirm_protocol_change": True})
                self.assertEqual(switched.status_code, 200, switched.text)
                self.assertEqual(switched.json()["providerId"], ids[0])
                async with sessions() as session:
                    a = await read_provider_runtime_settings(session, ids[0])
                    self.assertEqual(a["api_key"], "synthetic-key-openai_responses")
                    self.assertEqual((a["organization"], a["project"], a["embedding_model"], a["embedding_dimensions"], a["native_search_enabled"]), ("", "", "", None, False))
                    for secret in await session.scalars(select(ProviderSecret)):
                        self.assertNotIn("synthetic", secret.api_key)
                        self.assertNotIn("synthetic", secret.extra_headers)
                    for audit in await session.scalars(select(ConfigChange)):
                        self.assertNotIn("synthetic", audit.key + audit.action)
                self.assertEqual((await client.put(path, json={"enabled": False})).status_code, 200)
                async with sessions() as session:
                    with self.assertRaisesRegex(ValueError, "disabled"):
                        await read_provider_runtime_settings(session, ids[0])
                    with self.assertRaisesRegex(ValueError, "Unknown"):
                        await read_provider_runtime_settings(session, "missing")
                    await bootstrap(session)
                persisted = (await client.get("/api/providers")).json()[0]
                self.assertEqual(persisted["name"], "renamed-openai_responses")
                self.assertFalse(persisted["enabled"])
                self.assertEqual((await client.delete(path)).status_code, 200)
                self.assertEqual((await client.put(path, json={"enabled": True})).status_code, 404)
                self.assertEqual([item["providerId"] for item in (await client.get("/api/providers")).json()], ids[1:])
                await self.exercise_anthropic_paths(client, sessions)
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()


if __name__ == "__main__":
    unittest.main()
