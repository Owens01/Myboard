"""Run one board stage on the RocketRide engine.

Needs `pip install rocketride`, plus ROCKETRIDE_URI (wss://api.rocketride.ai)
and ROCKETRIDE_APIKEY. Run from the repo root:

    python scripts/rr_stage.py summarize '{"title": "mailkit EOL"}'
"""
import asyncio
import os
import sys


async def run(stage, user):
    from rocketride import RocketRideClient
    from rocketride.schema import Question

    async with RocketRideClient(uri=os.environ.get('ROCKETRIDE_URI', 'wss://api.rocketride.ai'),
                                auth=os.environ['ROCKETRIDE_APIKEY']) as client:
        task = await client.use(filepath=f'pipelines/{stage}.pipe')
        q = Question(expectJson=True)
        q.addInstruction('Role', f'You are the {stage} agent of an autonomous board. '
                                 'Follow the input exactly and reply with JSON only.')
        q.addQuestion(user)
        resp = await client.chat(token=task['token'], question=q)
        await client.terminate(task['token'])
        print((resp.get('answers') or [''])[0])


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit('usage: rr_stage.py <summarize|decide|governance> <user-json>')
    asyncio.run(run(sys.argv[1], sys.argv[2]))
