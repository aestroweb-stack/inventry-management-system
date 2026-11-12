
#!/usr/bin/env python3
"""Safely test MYRAA production MCP CRUD and Gemini tool routing.

Default mode: read + PREPARE only, no database writes.
Explicit --execute: creates a uniquely named test-only product, confirms it,
checks stock updates and Gemini's generated update preview, then archives the
product. No live/customer product is ever selected by this script.

Example on Railway:
  /app/.venv/bin/python tools/test_myraa_mcp_write_e2e.py
  /app/.venv/bin/python tools/test_myraa_mcp_write_e2e.py --execute

Uses INVENTORY_MCP_API_KEY from the Railway service environment; never prints it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.parse import urlparse

DEFAULT_ORIGIN = "https://inventry-management-system-production-787ff64f.up.railway.app"
BASE_NAME = "MYRAA MCP TEST KURTA"
UPDATED_NAME = "MYRAA MCP VERIFIED KURTA"


class TestFailure(RuntimeError):
    pass


def decode_tool_result(result) -> dict:
    if getattr(result, "isError", False):
        raise TestFailure(
            "MCP tool returned an error: "
            + str(getattr(result, "content", ""))[:350]
        )

    data = getattr(result, "structuredContent", None)

    if isinstance(data, dict):
        inner = data.get("result")
        if isinstance(inner, dict):
            return inner
        if data:
            return data

    for content in getattr(result, "content", []):
        if getattr(content, "type", "") == "text":
            try:
                parsed = json.loads(content.text)
                if isinstance(parsed, dict):
                    return (
                        parsed.get("result", parsed)
                        if isinstance(parsed.get("result"), dict)
                        else parsed
                    )
            except (ValueError, TypeError):
                pass

    raise TestFailure("MCP result was not a JSON dictionary.")


async def invoke(session, name: str, arguments: dict) -> dict:
    response = await session.call_tool(name, arguments=arguments)
    parsed = decode_tool_result(response)

    if parsed.get("status") == "error":
        raise TestFailure(
            f"{name}: {str(parsed.get('message', 'tool error'))[:350]}"
        )

    return parsed


def expect(condition, message: str) -> None:
    if not condition:
        raise TestFailure(message)


def variant_stock(product: dict, size: str = "M") -> int:
    matches = [
        v for v in product.get("sizes", [])
        if str(v.get("size", "")).upper() == size
    ]

    expect(
        len(matches) == 1,
        f"Expected exactly one {size} variant"
    )

    return int(matches[0]["current_stock"])


async def prepare(
    session,
    name: str,
    arguments: dict,
    expected_action: str,
    expected_sku: str
) -> str:

    outcome = await invoke(session, name, arguments)

    expect(
        outcome.get("status") == "awaiting_user_confirmation",
        f"{name} did not prepare a confirmation preview: {outcome}"
    )

    expect(
        outcome.get("action") == expected_action,
        f"Unexpected action from {name}"
    )

    details = outcome.get("details") or {}

    expect(
        details.get("sku") == expected_sku,
        f"Unexpected SKU from {name}; refusing to confirm"
    )

    token = outcome.get("confirmation_token")

    expect(
        isinstance(token, str) and len(token) > 25,
        f"No confirmation token from {name}"
    )

    return token


async def confirm(session, token: str) -> dict:
    result = await invoke(
        session,
        "confirm_inventory_action",
        {"confirmation_token": token}
    )

    expect(
        result.get("status") == "completed",
        "Confirmation did not complete"
    )

    return result


async def gemini_prepare_update(sku: str) -> dict:
    """Use the same Gemini service and tool dispatcher as MYRAA chat.

    This function asks Gemini to PREPARE an update.
    Actual confirmation is handled separately.
    """

    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise TestFailure(
            "GEMINI_API_KEY missing in Railway container"
        )

    # Correct backend import path.
    backend = Path(__file__).resolve().parents[1] / "backend"

    if not backend.exists():
        raise TestFailure(
            "Run this script from repository tools/ folder "
            "alongside backend/"
        )

    sys.path.insert(0, str(backend))

    # Import must remain INSIDE this function.
    from services import cloud_assistant_service # pyright: ignore[reportMissingImports]

    if cloud_assistant_service.provider_name() != "gemini":
        raise TestFailure(
            "MYRAA_CLOUD_PROVIDER is not gemini"
        )

    reply = await cloud_assistant_service.chat(
        [
            {
                "role": "user",
                "content": (
                    f"For the existing product SKU {sku}, "
                    f"update ONLY its product name to "
                    f"'{UPDATED_NAME}'. "
                    "Use the inventory update tool and show "
                    "me the confirmation preview. "
                    "Do not change any other SKU or stock."
                )
            }
        ],
        language="en-IN"
    )

    pending = reply.get("pending_action")

    expect(
        isinstance(pending, dict),
        "Gemini did not prepare a change (no pending_action)"
    )

    expect(
        pending.get("action") == "product_update",
        "Gemini prepared the wrong action; refusing to confirm"
    )

    details = pending.get("details") or {}

    expect(
        details.get("sku") == sku,
        "Gemini targeted another SKU; refusing to confirm"
    )

    changes = details.get("changes") or {}

    expect(
        changes == {"name": UPDATED_NAME},
        "Gemini proposed unexpected extra/incorrect changes; refusing to confirm"
    )

    token = pending.get("confirmation_token")

    expect(
        isinstance(token, str) and len(token) > 25,
        "Gemini pending_action lacks confirmation token"
    )

    return pending


async def run(origin: str, execute: bool) -> int:

    key = os.getenv("INVENTORY_MCP_API_KEY", "").strip()

    expect(
        len(key) >= 32,
        "INVENTORY_MCP_API_KEY must be configured"
    )

    expect(
        len(os.getenv("MYRAA_CONFIRMATION_SECRET", "")) >= 32,
        "MYRAA_CONFIRMATION_SECRET must be configured"
    )

    parsed = urlparse(origin)

    expect(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/"),
        "Pass an HTTPS origin without credentials or paths"
    )

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    sku = "MYRAA-E2E-" + uuid.uuid4().hex[:10].upper()
    endpoint = origin.rstrip("/") + "/api/mcp/"

    print("MCP endpoint:", endpoint)
    print(
        "Mode:",
        "EXPLICIT WRITE TEST + AUTO ARCHIVE"
        if execute
        else "SAFE PREVIEW ONLY (NO WRITES)"
    )
    print("Isolated test SKU:", sku)

    created = False
    cleanup_ok = False

    checks: list[str] = []
    errors: list[str] = []

    async with streamablehttp_client(
        endpoint,
        headers={"Authorization": "Bearer " + key},
        timeout=30,
        sse_read_timeout=120
    ) as (read, write, _):

        async with ClientSession(read, write) as session:

            await session.initialize()

            tool_names = {
                t.name for t in (await session.list_tools()).tools
            }

            required = {
                "get_product",
                "get_inventory_summary",
                "prepare_create_product",
                "prepare_update_product",
                "prepare_change_stock",
                "prepare_archive_product",
                "confirm_inventory_action"
            }

            expect(
                required.issubset(tool_names),
                "MCP tools missing: "
                + ", ".join(sorted(required - tool_names))
            )

            print("PASS MCP authenticated tool discovery")

            await invoke(
                session,
                "get_inventory_summary",
                {}
            )

            print("PASS MCP inventory read")

            original = await invoke(
                session,
                "get_product",
                {"sku": sku}
            )

            expect(
                "error" in original,
                "Random test SKU already exists; refusing to touch it"
            )

            token = await prepare(
                session,
                "prepare_create_product",
                {
                    "sku": sku,
                    "name": BASE_NAME,
                    "sizes": ["M"],
                    "category": "TEST ONLY"
                },
                "product_create",
                sku
            )

            check = await invoke(
                session,
                "get_product",
                {"sku": sku}
            )

            expect(
                "error" in check,
                "PREPARE changed database before confirmation"
            )

            print(
                "PASS CREATE preview: "
                "product absent before confirmation"
            )

            checks.append("create preview")

            if not execute:
                print(
                    "SAFE PREVIEW COMPLETE: "
                    "no database writes and no Gemini API call."
                )
                print(
                    "To test writes intentionally, "
                    "run again with --execute."
                )
                return 0

            try:
                # CREATE
                await confirm(session, token)
                created = True

                live = await invoke(
                    session,
                    "get_product",
                    {"sku": sku}
                )

                expect(
                    live.get("sku") == sku
                    and variant_stock(live) == 0,
                    "CREATE verification failed"
                )

                print("PASS CREATE confirmed + READ verified")
                checks.append("create + read")

                # GEMINI UPDATE
                try:
                    gemini_pending = await gemini_prepare_update(sku)

                    before = await invoke(
                        session,
                        "get_product",
                        {"sku": sku}
                    )

                    expect(
                        before.get("name") == BASE_NAME,
                        "Gemini changed database before confirmation"
                    )

                    print(
                        "PASS GEMINI tool routing: "
                        "preview generated, no early write"
                    )

                    await confirm(
                        session,
                        gemini_pending["confirmation_token"]
                    )

                    after = await invoke(
                        session,
                        "get_product",
                        {"sku": sku}
                    )

                    expect(
                        after.get("name") == UPDATED_NAME,
                        "Gemini UPDATE confirmation failed"
                    )

                    print(
                        "PASS GEMINI -> preview -> "
                        "MCP confirm -> UPDATE verified"
                    )

                    checks.append("Gemini -> update")

                except Exception as exc:
                    errors.append(
                        "Gemini end-to-end: " + str(exc)[:330]
                    )

                    print(
                        "FAIL Gemini E2E (test SKU only):",
                        str(exc)[:330]
                    )

                # STOCK SET
                variant = sku + "_M"

                t = await prepare(
                    session,
                    "prepare_change_stock",
                    {
                        "variant_code": variant,
                        "operation": "set",
                        "quantity": 7
                    },
                    "stock_change",
                    sku
                )

                live = await invoke(
                    session,
                    "get_product",
                    {"sku": sku}
                )

                expect(
                    variant_stock(live) == 0,
                    "Stock changed during PREPARE"
                )

                await confirm(session, t)

                live = await invoke(
                    session,
                    "get_product",
                    {"sku": sku}
                )

                expect(
                    variant_stock(live) == 7,
                    "Stock SET confirmation failed"
                )

                print("PASS UPDATE stock: set=7")
                checks.append("stock set")

                # STOCK ADD
                t = await prepare(
                    session,
                    "prepare_change_stock",
                    {
                        "variant_code": variant,
                        "operation": "add",
                        "quantity": 3
                    },
                    "stock_change",
                    sku
                )

                await confirm(session, t)

                live = await invoke(
                    session,
                    "get_product",
                    {"sku": sku}
                )

                expect(
                    variant_stock(live) == 10,
                    "Stock ADD confirmation failed"
                )

                print("PASS UPDATE stock: add=3 (now 10)")
                checks.append("stock add")

            except Exception as exc:
                errors.append(
                    "MCP CRUD: " + str(exc)[:330]
                )

                print(
                    "FAIL MCP CRUD:",
                    str(exc)[:330]
                )

            finally:
                # Archive only generated test SKU.
                try:
                    current = await invoke(
                        session,
                        "get_product",
                        {"sku": sku}
                    )

                    if current.get("sku") == sku:
                        cleanup_token = await prepare(
                            session,
                            "prepare_archive_product",
                            {"sku": sku},
                            "product_archive",
                            sku
                        )

                        await confirm(
                            session,
                            cleanup_token
                        )

                        archived = await invoke(
                            session,
                            "get_product",
                            {"sku": sku}
                        )

                        expect(
                            "error" in archived,
                            "ARCHIVE verification failed"
                        )

                        print(
                            "PASS DELETE/archive: "
                            "test SKU moved to Trash"
                        )

                        checks.append("archive")
                        cleanup_ok = True

                    else:
                        cleanup_ok = created

                except Exception as cleanup_exc:
                    errors.append(
                        "Cleanup: " + str(cleanup_exc)[:330]
                    )

                    print(
                        "WARNING cleanup failed:",
                        str(cleanup_exc)[:330]
                    )
                    print(
                        "MANUAL CLEANUP: archive ONLY test SKU",
                        sku
                    )

    print("\nTests passed:", ", ".join(checks))

    if cleanup_ok:
        print("Test SKU archived in Trash:", sku)

    if errors:
        print("Tests requiring attention:")

        for error in errors:
            print(" -", error)

        return 1

    print(
        "PASS: MCP CRUD and Gemini "
        "end-to-end checks succeeded."
    )

    print(
        "Separate browser check still needed: "
        "MYRAA chat's visible Confirm change button."
    )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "origin",
        nargs="?",
        default=DEFAULT_ORIGIN
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly allow test writes and auto-archive"
    )

    options = parser.parse_args()

    try:
        return asyncio.run(
            run(options.origin, options.execute)
        )
    except Exception as exc:
        print(
            "FAIL:",
            type(exc).__name__,
            str(exc)[:360]
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
