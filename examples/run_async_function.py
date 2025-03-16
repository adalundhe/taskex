import asyncio
import time

from taskex import Env, TaskRunner


async def async_task():
    loop = asyncio.get_event_loop()
    idx = 0

    start = time.monotonic()
    elapsed = 0

    while elapsed < 60:
        idx += 1
        await asyncio.sleep(1)

        loop.run_in_executor(
            None,
            print,
            str(idx),
        )

        elapsed = time.monotonic() - start


async def run():
    runner = TaskRunner(0, Env())

    run = runner.run(
        async_task,
    )

    await runner.wait(run.token)

    await runner.shutdown()


asyncio.run(run())
