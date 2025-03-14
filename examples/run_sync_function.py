import asyncio
from taskex import TaskRunner, Env

def run_process():
    import time
    
    start = time.monotonic()
    elapsed = 0
    idx = 0

    while elapsed < 60:
        time.sleep(1)
        idx += 1

        with open('test.txt', 'a') as file:
            file.write(str(idx) + '\n')
        

        elapsed = time.monotonic() - start

async def run():
    runner = TaskRunner(0, Env())

    run = runner.run(
        run_process,
    )

    await runner.wait('run_process', run.run_id)

    await runner.shutdown()


asyncio.run(run())

