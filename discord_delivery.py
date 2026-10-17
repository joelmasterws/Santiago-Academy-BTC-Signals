"""Discord webhook rate-limit handling for acknowledged posts (wait=true).

Only retries explicit HTTP 429 replies. An ambiguous network error is deliberately
not retried because Discord might already have accepted the message.
"""
import json
import time
import requests

MAX_ATTEMPTS = 12
MAX_WAIT_SECONDS = 240


def acknowledged_post(session, webhook, *, json_body=None, payload=None, image=None,
                      filename=None, timeout=45, sleep=time.sleep):
    start = time.monotonic()
    for attempt in range(MAX_ATTEMPTS):
        args = {'params': {'wait': 'true'}, 'timeout': timeout}
        if image is not None:
            image.seek(0)
            args['data'] = {'payload_json': json.dumps(payload)}
            args['files'] = {'files[0]': (filename, image, 'image/png')}
        else:
            args['json'] = json_body
        # Do not retry ambiguous connection errors: response may have been delivered.
        response = session.post(webhook, **args)
        if response.status_code != 429:
            response.raise_for_status()
            msg_id = response.json().get('id')
            if not msg_id:
                raise RuntimeError('Discord did not acknowledge message ID; delivery is ambiguous')
            return str(msg_id)
        try:
            body = response.json()
        except ValueError:
            body = {}
        retry_after = body.get('retry_after') or response.headers.get('Retry-After') or 2
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 2
        # Discord retry_after is seconds, including decimal seconds.
        delay = max(0.5, min(delay + 0.25, 65.0))
        if time.monotonic() - start + delay > MAX_WAIT_SECONDS or attempt == MAX_ATTEMPTS - 1:
            raise RuntimeError('Discord 429 retry budget exhausted; no new message acknowledged')
        print(f'DISCORD_RATE_LIMIT retry={attempt+1} wait_seconds={delay:.2f}', flush=True)
        sleep(delay)
    raise RuntimeError('Discord webhook retry attempts exhausted')
