"""Tahoe-LAFS CRS codec adapter for bounded stripe operations."""

from __future__ import annotations

from twisted.internet import defer, task

from allmydata.codec import CRSDecoder, CRSEncoder

K = 2
N = 4
STRIPE_CAPACITY = 128 * 1024


async def encode_data(data: bytes) -> tuple[list[bytes], int]:
    if not data or len(data) > STRIPE_CAPACITY:
        raise ValueError("each non-empty stripe must be at most 128 KiB")
    share_size = (len(data) + K - 1) // K
    padded = data.ljust(share_size * K, b"\0")
    input_shares = [padded[i * share_size:(i + 1) * share_size] for i in range(K)]
    encoder = CRSEncoder()
    encoder.set_params(len(data), K, N)
    shares, _share_ids = await encoder.encode(input_shares, list(range(N)))
    return shares, share_size


async def decode_data(shares: list[bytes], share_ids: list[int], data_size: int) -> bytes:
    if len(shares) != K or len(share_ids) != K or len(set(share_ids)) != K:
        raise ValueError("decode requires two distinct authenticated Tahoe shares")
    decoder = CRSDecoder()
    decoder.set_params(data_size, K, N)
    decoded = await decoder.decode(shares, share_ids)
    return b"".join(decoded)[:data_size]


def run_codec(awaitable):
    """Run one public Tahoe Deferred-based codec operation in its own CLI process."""
    result = []
    try:
        task.react(lambda _reactor: defer.Deferred.fromCoroutine(awaitable).addCallback(result.append))
    except SystemExit as exit_status:
        if exit_status.code != 0:
            raise
    if not result:
        raise RuntimeError("Tahoe codec reactor stopped without a result")
    return result[0]
