"""Explicit LIMIT/cancel transport, used only by the guarded execution core.

The live worker shares the collector's single TokenManager.
No write retry or redirects.
"""

import re

import httpx

from veyquant.adapters.toss import REST_URL, TossError, TossHTTPError
from veyquant.execution import DispatchVeto, Intent


class TossExecutionTransport:
    def __init__(self, tokens, http, account_seq):
        if not isinstance(account_seq, str) or not re.fullmatch(r"[0-9]+", account_seq):
            raise ValueError("invalid_account")
        self.tokens, self.http, self.account_seq = tokens, http, account_seq

    async def _post(self, path, body, before_send=None):
        headers = {
            "Authorization": f"Bearer {await self.tokens.get()}",
            "X-Tossinvest-Account": self.account_seq,
        }
        if before_send is not None and not before_send():
            raise DispatchVeto("final_check_rejected")
        try:
            response = await self.http.post(
                REST_URL + path, json=body, headers=headers, follow_redirects=False
            )
        except httpx.HTTPError:
            raise TossError("order_transport_uncertain") from None
        if response.status_code != 200:
            raise TossHTTPError(response.status_code)
        try:
            result = response.json()["result"]
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise TossError("order_response_uncertain") from None

    async def create(self, intent: Intent, *, before_send):
        return await self._post("/api/v1/orders", intent.body(), before_send)

    async def cancel(self, order_id):
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise ValueError("invalid_order_id")
        return await self._post(f"/api/v1/orders/{order_id}/cancel", {})
