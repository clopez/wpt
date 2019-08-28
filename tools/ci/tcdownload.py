import argparse
import os
import logging
import requests
import github


logging.basicConfig()
logger = logging.getLogger("tc-download")


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", action="store", default="master",
                        help="Branch (in the GitHub repository) or commit to fetch logs for")
    parser.add_argument("--user", action="store",
                        help="User name for staging.wpt.fyi", required=True)
    parser.add_argument("--password", action="store",
                        help="User password for staging.wpt.fyi", required=True)
    parser.add_argument("--server-url", action="store",
                        help="Base server url, defaults to https://staging.wpt.fyi", default="https://staging.wpt.fyi")
    parser.add_argument("--filter-artifact", action="store",
                        help="Only get artifacts that contain this string", default=None)
    parser.add_argument("--repo-name", action="store", default="web-platform-tests/wpt",
                        help="GitHub repo name in the format owner/repo. "
                        "This must be the repo from which the TaskCluster run was scheduled "
                        "(for PRs this is the repo into which the PR would merge)")
    parser.add_argument("--token-file", action="store",
                        help="File containing GitHub token")
    parser.add_argument("--out-dir", action="store", default=".",
                        help="Path to save the logfiles")
    return parser


def get_json(url, key=None):
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()
    if key:
        data = data[key]
    return data



def run(*args, **kwargs):
    if not os.path.exists(kwargs["out_dir"]):
        os.mkdir(kwargs["out_dir"])

    if kwargs["token_file"]:
        with open(kwargs["token_file"]) as f:
            gh = github.Github(f.read().strip())
    else:
        gh = github.Github()

    repo = gh.get_repo(kwargs["repo_name"])
    commit = repo.get_commit(kwargs["ref"])
    statuses = commit.get_statuses()
    taskgroups = set()

    for status in statuses:
        if not status.context.startswith("Taskcluster "):
            continue
        if status.state == "pending":
            continue
        taskgroup_id = status.target_url.rsplit("/", 1)[1]
        taskgroups.add(taskgroup_id)

    if not taskgroups:
        logger.error("No complete TaskCluster runs found for ref %s" % kwargs["ref"])
        return

    results = []
    screenshoots = 0
    wptresults = 0
    for taskgroup in taskgroups:
        taskgroup_url = "https://queue.taskcluster.net/v1/task-group/%s/list"
        artifacts_list_url = "https://queue.taskcluster.net/v1/task/%s/artifacts"
        tasks = get_json(taskgroup_url % taskgroup, "tasks")
        for task in tasks:
            task_id = task["status"]["taskId"]
            url = artifacts_list_url % (task_id,)
            if kwargs["filter_artifact"] is not None:
                if kwargs["filter_artifact"] not in task["task"]["metadata"]["name"]:
                    print("Skipping artifacts for %s" % task["task"]["metadata"]["name"])
                    continue
            print("Collecting artifacts for %s" % task["task"]["metadata"]["name"])
            for artifact in get_json(url, "artifacts"):
                artifact_url = "%s/%s" % (url, artifact["name"])
                if artifact["name"].endswith("wpt_report.json.gz"):
                    results.append(("result_url", artifact_url))
                    wptresults += 1
                if artifact["name"].endswith("wpt_screenshot.txt.gz"):
                    results.append(("screenshot_url", artifact_url))
                    screenshoots += 1

    if wptresults == 0:
        print("Not sending data, collected artifacts/wptresults is zero")
        return 1
    else:
        print("Sending a total of %s wptresults and %s screenshoots to %s" % (wptresults, screenshoots, kwargs["server_url"]))

        received = requests.post("%s/api/results/upload" % kwargs["server_url"], data=results,
                  auth=(kwargs["user"],kwargs["password"]))
        if received.status_code != 200:
            print("The server returned an unexpected code")
            print("Status code is: %s" % received.status_code)
            print("Content is: %s" % received.content)
            return 1
        if "added to queue" not in received.content:
            print("The server returned an unexpected content")
            print("Content is: %s" % received.content)
            return 1
        print(received.content)
        print("Check status in: %s/results/?run_id=%s" %(kwargs["server_url"],received.content.split(" ")[1]))
        return 0


def main():
    kwargs = get_parser().parse_args()

    run(None, vars(kwargs))


if __name__ == "__main__":
    main()
