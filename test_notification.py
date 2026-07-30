import asyncio
from monitor import notify

async def main():
    ok = await notify(
        "Odyssey monitor test",
        "Your Railway ntfy integration is working. Tap to open AMC.",
        click="https://www.amctheatres.com/movies/the-odyssey-76238/showtimes",
        priority="urgent",
        tags="white_check_mark,movie_camera",
    )
    raise SystemExit(0 if ok else 1)

asyncio.run(main())
