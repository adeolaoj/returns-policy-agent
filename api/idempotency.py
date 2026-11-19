"""Idempotency: makes "just retry" safe for writes.

A caller that times out can't tell whether the server never got the request or did it and the
reply was lost. So it retries with the same Idempotency-Key, and the server replays its first
answer instead of acting twice.

The rules (decided in step 3):
- Keys are scoped per endpoint: the same key on /refunds and /escalations is two requests.
- Bodies are compared parsed (model_dump), not as bytes, so key order and 68.0 vs. 68.00 don't
  make two requests look different.
- Same key, different body: refuse with 409. It almost always means a caller bug.
- Only successes are stored. A failure runs again on retry: a permanent one gives the same
  answer anyway, and a temporary one (out of stock, a 500) might succeed.

Known limitation: two *concurrent* requests with the same key could both act, because nothing
locks between replay() and save(). A real service would reserve the key first.
"""
from fastapi import HTTPException
from pydantic import BaseModel


class IdempotencyStore:
    def __init__(self) -> None:
        # (endpoint, key) -> (the request as model_dump(), the response that was sent)
        self._results: dict[tuple[str, str], tuple[dict, BaseModel]] = {}

    def replay(self, endpoint: str, key: str, request: BaseModel) -> BaseModel | None:
        """The stored response for a retry, or None for a new request (the route should act).
        Raises a 409 with code "idempotency_key_reused" if the key was used with a different body.
        """
        # A dict lookup finds the key directly, instead of looping over every stored key.
        stored = self._results.get((endpoint, key))
        if stored is None:
            return None

        stored_request, stored_response = stored
        if stored_request != request.model_dump():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_key_reused",
                    "message": f"Idempotency-Key {key!r} was already used with a different body."
                }
            )
        return stored_response

    def save(self, endpoint: str, key: str, request: BaseModel, response: BaseModel) -> None:
        """Remember a successful response. Routes call this only after they've acted."""
        self._results[(endpoint, key)] = (request.model_dump(), response)
