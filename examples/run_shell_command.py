import asyncio
from taskex import TaskRunner, Env, ShellProcess

async def run():
    runner = TaskRunner(0, Env())

    run = runner.command(
        'ls',
        alias='get_files',
        shell=True,
    )

    output: ShellProcess = await runner.wait(run.task_name, run.run_id)

    print(output.result)

    await runner.shutdown()


asyncio.run(run())

