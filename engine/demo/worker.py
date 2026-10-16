"""Poll the demo and grade posts through the configured BAML judge."""

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from baml_sdk import CandidatePost
from harness.post_judge.judge import grade_post


def request(base: str, token: str, path: str, payload: dict):
    req = Request(base.rstrip('/') + path, json.dumps(payload).encode(),
                  {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def main():
    load_dotenv()
    base, token = os.environ['DEMO_URL'], os.environ['DEMO_WORKER_TOKEN']
    while True:
        try:
            job = request(base, token, '/internal/claim', {})['job']
        except (URLError, TimeoutError, OSError):
            print('[worker] Demo unreachable; retrying.', flush=True)
            time.sleep(5)
            continue
        if not job:
            time.sleep(2)
            continue
        started = time.monotonic()
        failed = False
        try:
            result = grade_post(CandidatePost.model_validate(job['candidate'])).model_dump(mode='json')
            result['elapsed_seconds'] = round(time.monotonic() - started, 1)
        except Exception as exc:
            failed = True
            result = {'error': 'The judge could not finish this evaluation. Please try again.', 'error_type': type(exc).__name__}
            print(f'[worker] Judge failed: {type(exc).__name__}', flush=True)
        # Retrying completion uses the same lease; it never repeats the model request.
        for attempt in range(3):
            try:
                request(base, token, '/internal/finish', {'id': job['id'], 'lease': job['lease'], 'result': result, 'failed': failed})
                break
            except (URLError, TimeoutError, OSError):
                time.sleep(2)
        print(f'[worker] Evaluation {"failed" if failed else "completed"} in {time.monotonic()-started:.1f}s', flush=True)


if __name__ == '__main__':
    main()
