"""Stream one file to disk with resume support (HTTP Range) and progress callbacks."""
from pathlib import Path

import httpx

CHUNK = 1 << 16


async def fetch(resolved, dest: Path, on_progress, timeout_s: int = 60) -> None:
    """Download resolved.url to dest. Partial data lives in dest.part until complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0

    headers = dict(resolved.headers)
    if have:
        headers["Range"] = f"bytes={have}-"

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(timeout_s), cookies=resolved.cookies
    ) as client:
        async with client.stream("GET", resolved.url, headers=headers) as r:
            if r.status_code == 416 and have:  # nothing left to fetch
                part.replace(dest)
                return
            r.raise_for_status()
            if have and r.status_code != 206:  # server ignored Range: start over
                have = 0
            length = r.headers.get("content-length")
            total = int(length) + have if length else None

            done = have
            on_progress(done, total)
            with open(part, "ab" if have else "wb") as f:
                async for chunk in r.aiter_bytes(CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    on_progress(done, total)

    if total and done < total:
        raise IOError(f"connection closed early ({done}/{total} bytes)")
    part.replace(dest)
