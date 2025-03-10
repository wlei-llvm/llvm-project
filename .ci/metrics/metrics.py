import requests
import time
import os
from dataclasses import dataclass
import sys
import logging

import github
from github import Github
from github import Auth

GRAFANA_URL = (
    "https://influx-prod-13-prod-us-east-0.grafana.net/api/v1/push/influx/write"
)
GITHUB_PROJECT = "llvm/llvm-project"
WORKFLOWS_TO_TRACK = ["LLVM Premerge Checks"]
SCRAPE_INTERVAL_SECONDS = 5 * 60


@dataclass
class JobMetrics:
    job_name: str
    queue_time: int
    run_time: int
    status: int
    created_at_ns: int
    workflow_id: int
    workflow_name: str


@dataclass
class GaugeMetric:
    name: str
    value: int
    time_ns: int


def get_sampled_workflow_metrics(github_repo: github.Repository):
    """Gets global statistics about the Github workflow queue

    Args:
      github_repo: A github repo object to use to query the relevant information.

    Returns:
      Returns a list of GaugeMetric objects, containing the relevant metrics about
      the workflow
    """
    queued_job_counts = {}
    running_job_counts = {}

    # Other states are available (pending, waiting, etc), but the meaning
    # is not documented (See #70540).
    # "queued" seems to be the info we want.
    for queued_workflow in github_repo.get_workflow_runs(status="queued"):
        if queued_workflow.name not in WORKFLOWS_TO_TRACK:
            continue
        for queued_workflow_job in queued_workflow.jobs():
            job_name = queued_workflow_job.name
            # Workflows marked as queued can potentially only have some jobs
            # queued, so make sure to also count jobs currently in progress.
            if queued_workflow_job.status == "queued":
                if job_name not in queued_job_counts:
                    queued_job_counts[job_name] = 1
                else:
                    queued_job_counts[job_name] += 1
            elif queued_workflow_job.status == "in_progress":
                if job_name not in running_job_counts:
                    running_job_counts[job_name] = 1
                else:
                    running_job_counts[job_name] += 1

    for running_workflow in github_repo.get_workflow_runs(status="in_progress"):
        if running_workflow.name not in WORKFLOWS_TO_TRACK:
            continue
        for running_workflow_job in running_workflow.jobs():
            job_name = running_workflow_job.name
            if running_workflow_job.status != "in_progress":
                continue

            if job_name not in running_job_counts:
                running_job_counts[job_name] = 1
            else:
                running_job_counts[job_name] += 1

    workflow_metrics = []
    for queued_job in queued_job_counts:
        workflow_metrics.append(
            GaugeMetric(
                f"workflow_queue_size_{queued_job}",
                queued_job_counts[queued_job],
                time.time_ns(),
            )
        )
    for running_job in running_job_counts:
        workflow_metrics.append(
            GaugeMetric(
                f"running_workflow_count_{running_job}",
                running_job_counts[running_job],
                time.time_ns(),
            )
        )
    # Always send a hearbeat metric so we can monitor is this container is still able to log to Grafana.
    workflow_metrics.append(
        GaugeMetric("metrics_container_heartbeat", 1, time.time_ns())
    )
    return workflow_metrics


def get_per_workflow_metrics(
    github_repo: github.Repository, workflows_to_track: dict[str, int]
):
    """Gets the metrics for specified Github workflows.

    This function takes in a list of workflows to track, and optionally the
    workflow ID of the last tracked invocation. It grabs the relevant data
    from Github, returning it to the caller.

    Args:
      github_repo: A github repo object to use to query the relevant information.
      workflows_to_track: A dictionary mapping workflow names to the last
        invocation ID where metrics have been collected, or None to collect the
        last five results.

    Returns:
      Returns a list of JobMetrics objects, containing the relevant metrics about
      the workflow.
    """
    workflow_metrics = []

    workflows_to_include = set(workflows_to_track.keys())

    for workflow_run in iter(github_repo.get_workflow_runs()):
        if len(workflows_to_include) == 0:
            break

        if workflow_run.status != "completed":
            continue

        # This workflow was already sampled for this run, or is not tracked at
        # all. Ignoring.
        if workflow_run.name not in workflows_to_include:
            continue

        # There were no new workflow invocations since the previous scrape.
        # The API returns a sorted list with the most recent invocations first,
        # so we can stop looking for this particular workflow. Continue to grab
        # information on the other workflows of interest, if present.
        if workflows_to_track[workflow_run.name] == workflow_run.id:
            workflows_to_include.remove(workflow_run.name)
            continue

        workflow_jobs = workflow_run.jobs()
        if workflow_jobs.totalCount == 0:
            continue

        if (
            workflows_to_track[workflow_run.name] is None
            or workflows_to_track[workflow_run.name] == workflow_run.id
        ):
            workflows_to_include.remove(workflow_run.name)
        if (
            workflows_to_track[workflow_run.name] is not None
            and len(workflows_to_include) == 0
        ):
            break

        for workflow_job in workflow_jobs:
            created_at = workflow_job.created_at
            started_at = workflow_job.started_at
            completed_at = workflow_job.completed_at

            job_result = int(workflow_job.conclusion == "success")
            if job_result:
                # We still might want to mark the job as a failure if one of the steps
                # failed. This is required due to use setting continue-on-error in
                # the premerge pipeline to prevent sending emails while we are
                # testing the infrastructure.
                # TODO(boomanaiden154): Remove this once the premerge pipeline is no
                # longer in a testing state and we can directly assert the workflow
                # result.
                for step in workflow_job.steps:
                    if step.conclusion != "success" and step.conclusion != "skipped":
                        job_result = 0
                        break

            queue_time = started_at - created_at
            run_time = completed_at - started_at

            if run_time.seconds == 0:
                continue

            # The timestamp associated with the event is expected by Grafana to be
            # in nanoseconds.
            created_at_ns = int(created_at.timestamp()) * 10**9

            logging.info(
                f"Adding a job metric for job {workflow_job.id} in workflow {workflow_run.id}"
            )

            workflow_metrics.append(
                JobMetrics(
                    workflow_run.name + "-" + workflow_job.name,
                    queue_time.seconds,
                    run_time.seconds,
                    job_result,
                    created_at_ns,
                    workflow_run.id,
                    workflow_run.name,
                )
            )

    return workflow_metrics


def upload_metrics(workflow_metrics, metrics_userid, api_key):
    """Upload metrics to Grafana.

    Takes in a list of workflow metrics and then uploads them to Grafana
    through a REST request.

    Args:
      workflow_metrics: A list of metrics to upload to Grafana.
      metrics_userid: The userid to use for the upload.
      api_key: The API key to use for the upload.
    """

    if len(workflow_metrics) == 0:
        logging.info("No metrics found to upload.")
        return

    metrics_batch = []
    for workflow_metric in workflow_metrics:
        if isinstance(workflow_metric, GaugeMetric):
            name = workflow_metric.name.lower().replace(" ", "_")
            metrics_batch.append(
                f"{name} value={workflow_metric.value} {workflow_metric.time_ns}"
            )
        elif isinstance(workflow_metric, JobMetrics):
            name = workflow_metric.job_name.lower().replace(" ", "_")
            metrics_batch.append(
                f"{name} queue_time={workflow_metric.queue_time},run_time={workflow_metric.run_time},status={workflow_metric.status} {workflow_metric.created_at_ns}"
            )
        else:
            raise ValueError(
                f"Unsupported object type {type(workflow_metric)}: {str(workflow_metric)}"
            )

    request_data = "\n".join(metrics_batch)
    response = requests.post(
        GRAFANA_URL,
        headers={"Content-Type": "text/plain"},
        data=request_data,
        auth=(metrics_userid, api_key),
    )

    if response.status_code < 200 or response.status_code >= 300:
        logging.info(f"Failed to submit data to Grafana: {response.status_code}")


def main():
    # Authenticate with Github
    auth = Auth.Token(os.environ["GITHUB_TOKEN"])

    grafana_api_key = os.environ["GRAFANA_API_KEY"]
    grafana_metrics_userid = os.environ["GRAFANA_METRICS_USERID"]

    workflows_to_track = {}
    for workflow_to_track in WORKFLOWS_TO_TRACK:
        workflows_to_track[workflow_to_track] = None

    # Enter the main loop. Every five minutes we wake up and dump metrics for
    # the relevant jobs.
    while True:
        github_object = Github(auth=auth)
        github_repo = github_object.get_repo("llvm/llvm-project")

        current_metrics = get_per_workflow_metrics(github_repo, workflows_to_track)
        current_metrics += get_sampled_workflow_metrics(github_repo)

        upload_metrics(current_metrics, grafana_metrics_userid, grafana_api_key)
        logging.info(f"Uploaded {len(current_metrics)} metrics")

        for workflow_metric in reversed(current_metrics):
            if isinstance(workflow_metric, JobMetrics):
                workflows_to_track[
                    workflow_metric.workflow_name
                ] = workflow_metric.workflow_id

        time.sleep(SCRAPE_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
