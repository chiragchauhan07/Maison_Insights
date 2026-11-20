"""Vercel Daemon that consumes durable analysis requests from Vercel Queues."""
import asyncio
import logging
import os
from datetime import timedelta

from vercel.queue import Message, QueueClient, subscribe

from server import manager

logger = logging.getLogger("luxury_sentiment.worker")
QUEUE_TOPIC = "luxury-review-jobs"
QUEUE_GROUP = "maison-insights-worker"


@subscribe(topic=QUEUE_TOPIC, consumer_group=QUEUE_GROUP, retry_after=60)
async def process_analysis(message: Message[dict[str, str]]):
    """Process a queued analysis while the queue SDK renews its delivery lease."""
    job_id = (message.payload or {}).get("job_id")
    if not job_id:
        logger.error("Discarding a queue message with no job_id.")
        return
    await asyncio.to_thread(manager.process_queued_job, job_id)


async def main():
    region = os.getenv("QUEUE_REGION", "bom1")
    queue = QueueClient(region=region)
    logger.info("Maison Insights worker polling in Vercel Queue region %s.", region)
    await queue.poll_and_handle(
        process_analysis,
        interval=1.0,
        lease_duration=timedelta(hours=1),
    )


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
