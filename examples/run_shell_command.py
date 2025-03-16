import asyncio

from taskex import Env, ShellProcess, TaskRunner


async def run():
    runner = TaskRunner(0, Env())

    run = runner.command(
        "ls",
        alias="get_files",
        shell=True,
    )

    output: ShellProcess = await runner.wait(run.token)

    print(output.result)

    await runner.shutdown()


asyncio.run(run())
