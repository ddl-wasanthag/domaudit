import os
import logging
import requests
import datetime
import asyncio
from aiohttp import ClientSession,TCPConnector
from domaudit.services import constants
from flask import make_response

api_host = os.getenv('DOMINO_API_HOST')

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')

def api_fail(status_code, origin):
    """
    Error message for if we don't get the expected htp code
    """
    logging.error(f"An API error has occured whilst running {origin}. Status code: {status_code}")
    exit(1)

def api_skip(status_code,error, origin):
    """
    Error message for if we don't get the expected htp code
    """
    logging.warn(f"An API error has occured whilst running {origin}: {error}. Status code: {status_code}. Skipping call")

def get_project_id(project_name, project_owner, auth_header):
    """
    Returns if of a project
    """
    url = f"{api_host}/{constants.GATEWAY_ENDPOINT}/projects/findProjectByOwnerAndName"
    params = {"ownerName": project_owner,
              "projectName": project_name }
    result = requests.get(url, params=params, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "get_project_owner")
    project_id = result.json().get("id", None)
    return project_id    


def get_project_owner(project_id, auth_header):
    """
    Returns username of the owner of a project
    """
    url = f"{api_host}/{constants.GET_PROJECTS_ENDPOINT}/{project_id}"
    result = requests.get(url, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "get_project_owner")
    owner_username = result.json().get("ownerUsername", None)
    return owner_username


def get_jobs(project_id, auth_header, page_size, page_number):
    """
    This will return a list of all job IDs from the selected project.
    """
    url = f"{api_host}/{constants.JOBS_ENDPOINT}?projectId={project_id}&page_size={page_size}&page_no={page_number}&show_archived=true"
    result = requests.get(url, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "get_jobs")
    jobs = result.json().get("jobs", None)
    job_ids = []
    for job in jobs:
        job_ids.append(job.get("id", None))
    return job_ids


def get_job_data(job_id, auth_header):
    endpoints = [f"/{constants.JOBS_ENDPOINT}/{job_id}",
                 f"/{constants.JOBS_ENDPOINT}/{job_id}/runtimeExecutionDetails",
                 f"/{constants.JOBS_ENDPOINT}/{job_id}/comments",
                 f"/{constants.JOBS_ENDPOINT}/job/{job_id}/artifactsInfo"]
    job_data = {}
    for endpoint in endpoints:
        url = f"{api_host}{endpoint}"
        result = requests.get(url, headers=auth_header)
        if result.status_code != 200:
            api_fail(result.status_code, "get_job_data")
        if result.json() is not None:
            job_data.update(result.json())
    return job_data

async def get_async_api_data(endpoint, header, queue, session):
    """
    Asynchronously retrieve data from an API endpoint and put it in a queue
    """
    url = f"{api_host}{endpoint}"

    async with session.get(url) as response:
        if response.status != 200:
            api_skip(response.status, f"{url}", "get_async_api_data")
            await queue.put({})
        else:                
            data = await response.json()
            await queue.put(data)

async def get_job_data_async(job_id, project_id, auth_header, session):
    endpoints = [f"/{constants.JOBS_ENDPOINT}/{job_id}",
                 f"/{constants.JOBS_ENDPOINT}/{job_id}/runtimeExecutionDetails",
                 f"/{constants.JOBS_ENDPOINT}/{job_id}/comments",
                 f"/{constants.JOBS_ENDPOINT}/job/{job_id}/artifactsInfo",
                 f"/{constants.JOBS_ENDPOINT}/project/{project_id}/codeInfo/{job_id}"]
    job_data = {}

    queue = asyncio.Queue()
    # Create tasks for each endpoint to be processed asynchronously
    tasks = [get_async_api_data(endpoint, auth_header, queue, session) for endpoint in endpoints]
    await asyncio.gather(*tasks)  # Await all tasks

    while not queue.empty():
        job = await queue.get()
        if job:
            job_data.update(job)

    
    return job_data


def get_goals(project_id, auth_header):
    url = f"{api_host}/{constants.PROJECTMANAGEMENT_ENDPOINT}/{project_id}/goals"
    result = requests.get(url, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "get_goals")
    goals = {}
    for goal in result.json():
        goals[goal.get("id", None)] = goal.get("title", None)
    return goals


async def aggregate_job_data(job_ids, project_id, auth_header, threads):
    """
    Aggregate job data for multiple job IDs asynchronously
    """
    jobs = {}
    connector = TCPConnector(limit=threads)
    async with ClientSession(connector=connector,headers=auth_header) as session:  # Use a single session for all requests
        # Create tasks for each job ID
        tasks = [get_job_data_async(job_id, project_id, auth_header, session) for job_id in job_ids]
        results = await asyncio.gather(*tasks)  # Await all tasks

    # Update the jobs dictionary with the results
    for job in results:
        jobs[job.get("id", None)] = job
    return jobs


def convert_datetime(time_str):
    return datetime.datetime.fromtimestamp(time_str / 1e3, tz=datetime.timezone.utc).strftime('%F %X:%f %Z')


def generate_report(jobs, goals, project_name, project_owner, project_id, create_links, auth_header):
    tidy_jobs = {}
    # Pull domino hostname
    domino_host = api_host
    url = f"{api_host}/currentInstallConfig"
    result = requests.get(url, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "current_install_config")
    else:
        domino_host = result.json()['host']

    for job in jobs:
        tidy_jobs[job] = {}
        comments = []
        if jobs.get(job, None).get("comments", None):
            for comment_details in jobs.get(job, None).get("comments", '[]'):
                comment = {
                    'comment-username': comment_details.get("commenter", None).get("username", None),
                    'comment-timestamp': convert_datetime(comment_details.get("created", 0)),
                    'comment-value': comment_details.get("commentBody", None).get("value", None)
                }
                comments.append(comment)
        tidy_jobs[job]['Comments'] = comments
        git_repos = []
        if jobs.get(job, None).get("dependentRepositories", None):
            for repo in jobs.get(job, None).get("dependentRepositories", '[]'):
                repo_details = {
                    "Repo URI": repo.get("uri", None),
                    "Starting Branch": repo.get("startingBranch", None),
                    "Starting Commit ID ": repo.get("startingCommitId", None) ,
                    "Starting Commit URI ": repo.get("startingCommitUri", None)
                }
                # repo_uri = repo.get("uri", None)
                git_repos.append(repo_details)
        tidy_jobs[job]['Linked Repos'] = git_repos
        dataset_names = []
        if jobs.get(job, None).get("dependentDatasetMounts", None):
            for dataset in jobs.get(job, None).get("dependentDatasetMounts", '[]'):
                datasets = {
                    "Dataset Name": dataset.get("datasetName", None),
                    "Dataset Snapshot version": dataset.get("snapshotVersion", None)
                }
                # dataset_name = dataset.get("datasetName", None)
                dataset_names.append(datasets)
        tidy_jobs[job]['Datasets'] = dataset_names

        datavolume_names = []
        if jobs.get(job, None).get("dependentExternalVolumeMounts", None):
            for volume in jobs.get(job, None).get("dependentExternalVolumeMounts", '[]'):
                volume_info = {
                    "Volume Name": volume.get("name", None)
                }
                if volume.get("mount",None):
                    volume_info["Volume Mount Point"] = volume.get("mount").get("mountPath",None)
                    volume_info["Volume Read Only"] = volume.get("mount").get("readOnly",None)
                
                datavolume_names.append(volume_info)
        tidy_jobs[job]['External Volumes'] = datavolume_names
        
        goal_names = []
        if jobs.get(job, None).get("goalIds", None):
            for goal_id in jobs.get(job, None).get("goalIds", '[]'):
                goal_names.append(goals[goal_id])
        tidy_jobs[job]['Goals'] = goal_names
        tidy_jobs[job]['Job Number'] = jobs.get(job, None).get("number", None)  
        tidy_jobs[job]['Project Name'] = project_name
        endStateCommit = None
        if jobs.get(job):
            if jobs.get(job).get("endState"):
              endStateCommit = jobs.get(job).get("endState").get("commitId", None)
        tidy_jobs[job]["Commit ID"] = endStateCommit
        if jobs.get(job, None).get("mainRepo", None):
            main_repo_commit_url = jobs.get(job).get("mainRepo").get("commitResourceLink", None)
        else:
            commit_detail = jobs.get(job, {}).get("commitDetails", {})
            main_repo_commit = commit_detail.get("inputCommitId", None)
            if main_repo_commit:
                main_repo_commit_url = f"{domino_host}/u/{project_owner}/{project_name}/browse?commitId={main_repo_commit}"
            else:
                main_repo_commit_url = None
        if create_links:
            commit_url = f"{domino_host}/u/{project_owner}/{project_name}/browse?commitId={endStateCommit}"
            tidy_jobs[job]["Results Commit URL"] = commit_url
            tidy_jobs[job]["Main Repo Commit URL"] = main_repo_commit_url
            audit_url = f"{domino_host}/projects/{project_id}/auditLog"
            tidy_jobs[job]["Audit URL"] = audit_url
        tidy_jobs[job]["Command"] = jobs.get(job, None).get("jobRunCommand", None)
        tidy_jobs[job]["Hardware Tier"] = jobs.get(job, None).get("hardwareTier", None)
        tidy_jobs[job]["Username"] = jobs.get(job, None).get("startedBy", None).get("username", None)
        tidy_jobs[job]["Execution Status"] = jobs.get(job, None).get("statuses", None).get("executionStatus", None)
        tidy_jobs[job]["Submission Time"] = convert_datetime(jobs.get(job, 0).get("stageTime", 0).get("submissionTime", 0))
        if jobs.get(job, None).get("stageTime", None).get("runStartTime", None):
            tidy_jobs[job]["Run Start Time"] = convert_datetime(jobs.get(job, 0).get("stageTime", 0).get("runStartTime", 0))
        else:
            tidy_jobs[job]["Run Start Time"] = None
        tidy_jobs[job]["Completed Time"] = convert_datetime(jobs.get(job, 0).get("stageTime", 0).get("completedTime", 0))
        tidy_jobs[job]["Environment Name"] = jobs.get(job, None).get("environment", None).get("environmentName", None)
        tidy_jobs[job]["Environment Version"] = jobs.get(job, None).get("environment", None).get("revisionNumber", None)
        tidy_jobs[job]["Execution Status Completed"] = jobs.get(job, None).get("statuses", None).get("isCompleted", None)
        tidy_jobs[job]["Execution Status Archived"] = jobs.get(job, None).get("statuses", None).get("isArchived", None)
        tidy_jobs[job]["Execution Status Scheduled"] = jobs.get(job, None).get("statuses", None).get("isScheduled", None)
    return tidy_jobs

def get_project_activity(auth_header, requesting_user, args=None):
    
    if not "project_id" in args:
        logging.error(f"No project details have been supplied. Args sent: {args}")
        error = {
            "message": "Usage: /project_activity?project_id=<project_id>"
        }
        return make_response(error,400)
    
    logging.info(f"Args sent: {args}")

    project_id = args.get('project_id', None)
    page_size = args.get('page_size', 500)
    latest_event_time = args.get('latest_event_time',None)
    source = args.get('activity_source',None)

    logging.info(f"{requesting_user} requested activity report for {project_id}...")

    url = f"{api_host}/{constants.ACTIVITY_ENDPOINT}?projectId={project_id}&pageSize={page_size}"
    if latest_event_time:
        utc_time = datetime.datetime.strptime(latest_event_time, "%Y-%m-%d")
        epoch_time_ms = round((utc_time - datetime.datetime(1970, 1, 1)).total_seconds()) * 1000
        url = f"{url}&latestTimeStamp={epoch_time_ms}"
    
    if source:
        url = f"{url}&filterBy={source}"

    result = requests.get(url, headers=auth_header)
    if result.status_code != 200:
        api_fail(result.status_code, "get_project_activity")
    
    output = {}
    for activity in result.json()["activity"]:
        activityBy = activity.get("activityBy",None)
        commit_message = ""
        files_changed = ""
        run_id = ""
        file_action = ""
        status = ""
        if "metadata" in activity:
            data = activity["metadata"].get("data",{})

            commit_message = data.get("commitMessage","")
            files_changed = ",".join(data.get("filesChanged",[]))
            file_action = data.get("action","")
            status = data.get("currentStatus","")

        # TODO: what??
        # if activity["activitySource"] in ["job","workspace"]:
        #     run_id = activity["sourceId"]
        # elif "metadata" in activity:
        #     if activity["metadata"]["data"].get("fileChangedDueTo","") == "workspace"
         
        output[activity["timestamp"]] = {
            "Activity": activity["activity"],
            "Timestamp": convert_datetime(activity["timestamp"]),
            "User": activityBy['username'] if activityBy else "",
            "Source": activity["activitySource"],
            "Status": status,
            "CommitMessage": commit_message,
            "Action": file_action,
            "Files Changed": files_changed
        }
    return output


def main(auth_header, requesting_user, args=None):
    t0 = datetime.datetime.now()
    if not all(key in args for key in ("project_name","project_owner","project_id")):
        logging.error(f"No project details have been supplied. Args sent: {args}")
        error = {
            "message": "Usage: /project_audit?project_name=<project_name>&project_owner=<project_owner>&project_id=<project_id>"
        }
        return make_response(error,400)

    project_id = args.get('project_id', None)
    project_name = args.get('project_name', None)
    project_owner = args.get('project_owner', None)
    create_links = args.get('links', "False")
    page_size = args.get('page_size', 500)
    page_number = args.get('page_number', 1)
    create_links = True if create_links.lower() == "true" else False
    threads = int(args.get('thread_count',os.getenv("PROJECT_AUDIT_HTTP_THREAD_COUNT",10)))
    
    logging.info(f"Args sent: {args}")
    logging.info(f"{requesting_user} requested audit report for {project_name}...")

    goals = get_goals(project_id, auth_header)
    job_ids = get_jobs(project_id, auth_header, page_size, page_number)
    logging.info(f"Found {len(job_ids)} jobs to report. Aggregating job metadata...")
    logging.info(f"Attempting API queries using {threads} thread(s)...")
    t = datetime.datetime.now()    
    jobs = asyncio.run(aggregate_job_data(job_ids, project_id, auth_header, threads=threads))
    t = datetime.datetime.now() - t
    logging.info(f"Queries succeeded in {str(round(t.total_seconds(),1))} seconds.")     
    report_data = generate_report(jobs,goals,project_name, project_owner, project_id, create_links, auth_header)
    logging.info(f"Audit report generated in {str(round(t.total_seconds(),1))} seconds.")
    return report_data


if __name__ == '__main__':
    main()