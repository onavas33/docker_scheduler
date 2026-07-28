#!/usr/bin/env python3

"""Standalone Docker-based job scheduler using cron expressions and container labels.

This module is designed to run as its own Docker container. It scans running Docker
containers for job definitions specified through container labels, parses cron schedules,
and executes the associated commands using APScheduler.

Features:
    - Runs as an independent scheduler container.
    - Dynamically discovers job definitions from other containers via labels.
    - Validates cron expressions and logs any issues.
    - Executes commands inside the target containers at scheduled times.
    - Provides logging and basic error handling.

Usage:
    Build and run this module as a Docker container with access to the Docker socket
    (e.g., mount /var/run/docker.sock). It will automatically detect and manage jobs
    defined on labeled containers.

Example label format with container default user:
    scheduler.enable: "true"
    scheduler.<jobname>.schedule = "* * * * *"
    scheduler.<jobname>.command = "echo hello"

Example label format with explicit user:
    scheduler.enable: "true"
    scheduler.<jobname>.schedule = "* * * * *"
    scheduler.<jobname>.command = "whoami"
    scheduler.<jobname>.user = "nobody"
"""


import logging # for logs
import os # for docker socket check
import signal # for signal handlers SIGINT and SIGTERM
import sys # for sys.exit(0)
import threading # for scheduler and watcher threads
import time # for endles while loop with time.sleep(1)
import docker
# import python scheduler:
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Configure logging with timestamp
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)
# We don't want APscheduler INFO logs
logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
logging.getLogger('apscheduler.jobstores.default').setLevel(logging.WARNING)
logging.getLogger('apscheduler.triggers.cron').setLevel(logging.WARNING)

current_tz = os.environ.get('TZ', 'UTC')
time.tzset()
logger.info("Configured timezone for job scheduling: %s", current_tz)

try:
    if not os.environ.get('DOCKER_HOST') and not os.path.exists('/var/run/docker.sock'):
        logger.error(
            "Docker socket not found at /var/run/docker.sock and DOCKER_HOST is not set. Exiting."
        )
        sys.exit(1)
    docker_client = docker.from_env()
    docker_client.ping()
    logger.info("Connected to Docker daemon at %s", docker_client.api.base_url)
except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Cannot connect to Docker daemon: %s", e)
    sys.exit(1)


# Create and non-blocking scheduler
scheduler = BackgroundScheduler()

def handle_exit(signum, frame):  # pylint: disable=unused-argument
    """
    Handle signal SIGINT and SIGTERM by shutting down the scheduler and exiting.
    
    This function is registered as a signal handler for SIGINT and SIGTERM.
    When a signal is received, it logs the signal, shuts down the scheduler,
    and calls sys.exit(0) to exit the program.
    """
    logger.info("Received signal %s, shutting down...", signum)
    scheduler.shutdown(wait=False)
    sys.exit(0)

# Register signal handlers for SIGINT and SIGTERM
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def is_scheduler_enabled(container):
    """
    Check if the scheduler is enabled for the container.
    Returns: 
        - False whenever the label is "false" (or anything other than "true"), 
          including if the label is missing.
        - True when label is true.
    """
    labels = container.labels or {}
    return labels.get("scheduler.enable", "").lower() == "true"


def extract_raw_jobs(labels):
    """
    Collect raw schedule/command/user pairs from scheduler.<job> labels.
    Returns:
        - a dict with job names as keys and dicts with schedule, command and user
        as values.
    """
    raw_jobs = {}
    for key, value in (labels or {}).items():
        if not key.startswith("scheduler."):
            continue
        parts = key.split('.')
        if len(parts) != 3:
            continue  # skip keys like scheduler.enable
        _, job_name, prop = parts # unpacking values
            # parts[0] -> scheduler -> _ (ignore this value)
            # parts[1] -> backup -> save as job_name
            # parts[2] -> schedule -> save as prop
            # job_name = "backup"
            # prop = "schedule"
        if prop not in ("schedule", "command", "user"):
            continue
        # Ensure a dict exists for this job_name
        if job_name not in raw_jobs:
            raw_jobs[job_name] = {}
        raw_jobs[job_name][prop] = value
    return raw_jobs


def validate_jobs(container, raw_jobs):
    """
    Validate and build final job list:
      - must have both schedule and command
      - schedule must be valid cron expression
      - returns list of job dicts with id, container_name, container_id,
        schedule, command
    Returns:
        - list of job dicts
    """
    jobs = []
    for job_name, props in raw_jobs.items():
        schedule = props.get("schedule")
        command = props.get("command")
        user = props.get("user")
        if not schedule or not command:
            logger.warning(
                "Incomplete job for %s: %s - missing schedule or command",
                container.name,
                job_name
            )
            continue  # skip incomplete jobs
        try:
            CronTrigger.from_crontab(schedule)
        except ValueError:
            logger.warning(
                "Invalid schedule (not cron format) for %s:%s -> %s",
                container.name,
                job_name,
                schedule
            )
            continue # skip invalid cron expressions
        cont_short_id = container.id[:12]
        job_id = f"{cont_short_id}_{job_name}"
        jobs.append({
            "id": job_id,
            "container_name": container.name,
            "container_id": cont_short_id,
            "schedule": schedule,
            "command": command,
            "user": user
        })
    return jobs


def execute_job(job):
    """
    Execute the command in the given container and print the output.
    """
    cmd = job['command']
    user = job['user']
    cid = job['container_id']
    job_id = job['id']
    cont_name = job['container_name']
    display_user = user if user else "<default user>"
    logger.info("Running job %s in %s with user %s", job_id, cont_name, display_user)
    try:
        # run via shell to support redirection/pipes
        shell_cmd = ["/bin/sh", "-c", cmd]
        result = docker_client.containers.get(cid).exec_run(
            shell_cmd,
            tty=True,
            user=user or ""
        )
        output = result.output.decode('utf-8', errors='replace')
        exit_code = result.exit_code
        if exit_code != 0:
            # Log error without traceback
            logger.error(
                "Job %s in %s (%s) exited with code %s: %s",
                job_id, cont_name, cid, exit_code, output
            )
            return  # Do not raise to avoid traceback logging
        #logger.info(f"Output for {cont_name} ({job_id}):\n{output}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Log the exception without full traceback
        logger.exception(
            "Error running job %s in %s (%s): %s", job_id, cont_name, cid, e
        )
        return


def sync_container(container):
    """
    Sync APScheduler jobs for a single container based on its labels.
    """
    cont_id = container.id[:12]
    prefix = f"{cont_id}_"
    # Remove existing jobs for this container
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            scheduler.remove_job(job.id)
            logger.info("Removed job %s", job.id)
    # If disabled, do nothing
    if not is_scheduler_enabled(container):
        return
    # Extract and validate raw job definitions
    raw = extract_raw_jobs(container.labels)
    jobs = validate_jobs(container, raw)
    # Inform about resync
    logger.info("Resyncing jobs for container %s (%s)", container.name, cont_id)
    # Schedule new jobs
    for job in jobs:
        trigger = CronTrigger.from_crontab(job["schedule"])
        scheduler.add_job(
            execute_job,
            trigger=trigger,
            args=[job],
            id=job["id"],
            name=f"{job['container_name']}::{job['id']}"
        )
        display_user = job['user'] if job['user'] else "<default user>"
        logger.info(
            "Scheduled %s: %s %s as user %s", 
            job['id'], job['schedule'], job['command'], display_user
        )


def initial_sync():
    """
    Scan all running containers at startup and sync their jobs.
    """
    logger.info("Performing initial sync...")
    for container in docker_client.containers.list():
        sync_container(container)


def watch_events():
    """
    Listen to Docker events and resync or remove jobs based on container lifecycle.
    """
    for event in docker_client.events(decode=True, filters={"type": "container"}):
        action = event.get("Action")
        raw_id = event.get("Actor", {}).get("ID")
        if not raw_id:
            continue
        cid = raw_id[:12]
        # logger.info(f"Event {action} for container {cid}")
        # Attempt to fetch container; some events (destroy) may not find it
        try:
            cont = docker_client.containers.get(cid)
        except docker.errors.NotFound:
            cont = None
        # Set container name if exists, otherwise use the short container ID (cid)
        cont_name = cont.name if cont else cid

        # Actions that should (re)sync jobs: new start, unpause, rename, or config update
        if action in ("start", "update", "unpause") and cont:
            logger.info(
                "Sync jobs for %s (%s) due to '%s'", cont_name, cid, action
            )
            sync_container(cont)

        # Actions that should remove all jobs: stop, die, destroy, pause
        elif action in ("stop", "die", "destroy", "pause"):
            logger.info(
                "Removing jobs for %s (%s) due to '%s'", cont_name, cid, action
            )
            prefix = f"{cid}_"
            for job in scheduler.get_jobs():
                if job.id.startswith(prefix):
                    scheduler.remove_job(job.id)
                    logger.info("Removed job %s", job.id)


if __name__ == '__main__':
    # Start APScheduler in its own background thread
    scheduler_thread = threading.Thread(target=scheduler.start, daemon=True)
    scheduler_thread.start()
    logger.info("APScheduler background thread started")

    # Perform initial sync of existing containers
    initial_sync()

    # Start Docker events watcher thread
    watcher_thread = threading.Thread(target=watch_events, daemon=True)
    watcher_thread.start()
    logger.info("Event watcher thread started")

    logger.info("Scheduler service is running...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_exit(None, None)
