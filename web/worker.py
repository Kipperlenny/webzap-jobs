"""Queue worker: processes tasks whenever an LLM connector is online.

Run: python worker.py   (in docker compose: the `worker` service)
The LLM server may be offline for hours – tasks simply wait. Offline time never counts as a failed attempt.
"""

import logging
import signal
import time

from jobagent import connectors

import extract
import store

log = logging.getLogger("webzap-jobs.worker")
IDLE_SLEEP = 20  # seconds between queue checks when nothing is due
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def process(task: dict, conn: connectors.Connector, model: str):
    if task["kind"] != "extract":
        store.requeue_task(task, 0, f"unknown task kind {task['kind']}", count_attempt=True, max_attempts=1)
        return
    feedback = task.get("feedback") or []
    raw = conn.chat_json(
        model, extract.SYSTEM, extract.build_user_prompt(task["text"], feedback, task["lang"]), extract.SCHEMA
    )
    derived = extract.validate(raw)
    store.finish_task(task, derived, f"{conn.name}:{model}:v{extract.PROMPT_VERSION}:fb{len(feedback)}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    conns = connectors.configured()
    log.info("worker started with connectors: %s", [c.name for c in conns] or "none – tasks will wait")
    store.reset_running()
    if n := store.backfill(extract.PROMPT_VERSION):
        log.info("queued %d sign-up(s) for (re-)extraction", n)
    offline_checks = 0
    while not _stop:
        store.purge_expired()
        if not store.pending_count():
            time.sleep(IDLE_SLEEP)
            continue
        picked = connectors.pick(conns)
        if not picked:
            offline_checks += 1
            wait = connectors.backoff(offline_checks)
            log.info("%d task(s) waiting, no LLM online – next check in %ds", store.pending_count(), wait)
            _sleep(wait)
            continue
        offline_checks = 0
        conn, model = picked
        while not _stop and (task := store.claim_task()):
            started = time.time()
            try:
                process(task, conn, model)
                log.info(
                    "task %s (%s) done via %s in %.1fs", task["id"], task["kind"], conn.name, time.time() - started
                )
            except connectors.Unavailable as e:
                log.info("task %s: connector went offline (%s) – back to queue", task["id"], e)
                store.requeue_task(task, 60, str(e))
                break
            except (connectors.BadOutput, ValueError) as e:
                log.warning("task %s: unusable answer (%s) – retry later", task["id"], e)
                store.requeue_task(task, 300 * (task["attempts"] + 1), str(e), count_attempt=True)
            except Exception as e:  # noqa: BLE001 – never let one task kill the worker
                log.exception("task %s crashed", task["id"])
                store.requeue_task(task, 600, f"{e.__class__.__name__}: {e}", count_attempt=True)


def _sleep(seconds: float):
    end = time.time() + seconds
    while not _stop and time.time() < end:
        time.sleep(min(5, end - time.time()))


if __name__ == "__main__":
    main()
