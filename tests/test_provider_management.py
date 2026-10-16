"""Exercise the authenticated public API against isolated PostgreSQL schemas."""

import asyncio
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
from app.repository import bootstrap, read_provider_runtime_settings


@unittest.skipUnless(os.environ.get("VIREYA_TEST_DATABASE_URL"), "requires isolated PostgreSQL")
class ProviderManagementTest(unittest.TestCase):
    def test_authenticated_lifecycle_and_isolation(self):
        asyncio.run(self.exercise())

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
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()


if __name__ == "__main__":
    unittest.main()
