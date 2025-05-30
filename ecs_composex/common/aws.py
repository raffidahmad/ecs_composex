# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille <john@compose-x.io>

"""
Common functions and variables fetched from AWS.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from boto3.session import Session
    from ecs_composex.common.settings import ComposeXSettings
    from ecs_composex.common.stacks import ComposeXStack

import re
from copy import deepcopy
from datetime import datetime as dt
from time import sleep

from botocore.exceptions import ClientError
from compose_x_common.aws import get_assume_role_session, validate_iam_role_arn
from compose_x_common.aws.arns import ARNS_PER_TAGGINGAPI_TYPE
from compose_x_common.compose_x_common import keyisset
from tabulate import tabulate

from ecs_composex.common.logging import LOG
from ecs_composex.iam import ROLE_ARN_ARG


def get_cross_role_session(
    session: Session, arn: str, region_name: str = None, session_name: str = None
) -> Session:
    """
    Function to override ComposeXSettings session to specific session for Lookup
    """
    if not session_name:
        session_name = "ComposeX@Lookup"
    try:
        return get_assume_role_session(
            session, arn, session_name=session_name, region=region_name
        )
    except ClientError:
        LOG.error(f"Failed to use the Role ARN {arn}")
        raise


def define_lookup_role_from_info(info: dict, session: Session) -> Session:
    """
    Function to override ComposeXSettings session to specific session for Lookup
    """
    if not keyisset(ROLE_ARN_ARG, info):
        return session
    validate_iam_role_arn(info[ROLE_ARN_ARG])
    return get_cross_role_session(session, info[ROLE_ARN_ARG])


def set_filters_from_tags_list(tags: list) -> list:
    """
    Simple function to define the tags filters to use
    """
    filters = []
    filters_mapping = {}
    for tag in tags:
        for key, value in tag.items():
            if key not in filters_mapping.keys():
                if not isinstance(value, list):
                    filters_mapping[key] = [value]
                else:
                    filters_mapping[key] += value
            else:
                filters_mapping[key].append(value)
    for key, values in filters_mapping.items():
        filters.append({"Key": key, "Values": tuple(values)})
    return filters


def define_tagsgroups_filter_tags(tags: list[dict]) -> list:
    """
    Function to create the filters out of tags list
    """
    if isinstance(tags, list):
        return set_filters_from_tags_list(tags)
    elif isinstance(tags, dict):
        _tags = [
            {
                "Key": key,
                "Values": (str(values) if isinstance(values, int) else values,),
            }
            for key, values in tags.items()
            if isinstance(values, (list, str, int)) and isinstance(key, str)
        ]
        return _tags
    raise TypeError("Tags must be one of", [list, dict], "Got", type(tags))


def get_resources_from_tags(
    session: Session, aws_resource_search: str, search_tags: list
) -> Union[dict, None]:
    """
    Function to retrieve AWS Resources ARNs from the tags using the Resource Groups Tagging API
    """
    try:
        client = session.client("resourcegroupstaggingapi")
        resources_r = client.get_resources(
            ResourceTypeFilters=[aws_resource_search], TagFilters=search_tags
        )
        return resources_r
    except ClientError as error:
        LOG.error(error)
        LOG.error("Not processing this resource. Skipping")
        return None


def handle_multi_results(
    arns: list[str], name: str, res_type: str, regexp: str, allow_multi: bool = False
) -> Union[str, list[str]]:
    """
    Function to evaluate more than one result to see if we can match a unique name.

    :raises LookupError:
    :return: The ARN(s) of the resource matching the name. Supports to return multiple ARNs
    """
    found = 0
    found_arn = None
    re_finder = re.compile(regexp)
    for arn in arns:
        found_name = re_finder.match(arn).groups()[0]
        if found_name and found_name == name:
            found += 1
            found_arn = arn
    if found == 1:
        LOG.info(f"Matched {res_type} {name}")
        return found_arn
    elif not allow_multi and found > 1:
        raise LookupError(
            f"More than one result was found for {name} / {res_type} "
            "but could not match the name to a single resource."
            "Found",
            arns,
        )
    elif found == 0:
        raise LookupError(
            f"No {res_type} named {name} was found with the provided tags."
            " Found with provided tags",
            [re_finder.match(arn).groups()[0] for arn in arns],
        )
    elif allow_multi and found > 1:
        LOG.info(f"Found multiple resources for {res_type} and Name/Id {name}.")
        return arns


def handle_search_results(
    arns: list[str],
    name: str,
    res_types,
    aws_resource_search: str,
    allow_multi: bool = False,
) -> Union[str, list[str]]:
    """
    Function to parse tag resource search results

    """
    if not arns:
        raise LookupError(
            "No resources were found with the provided tags and information",
            name,
            aws_resource_search,
        )
    elif not allow_multi and len(arns) > 1:
        raise LookupError(
            f"More than one resource {name}:{aws_resource_search} was found with the current tags."
            "Found",
            arns,
        )
    elif allow_multi and len(arns) > 1:
        return arns
    else:
        return arns[0]


def validate_search_input(res_types: dict, res_type: str) -> None:
    """
    Function to validate the search query

    :raises: KeyError
    """

    if not isinstance(res_type, str):
        raise KeyError("type must be one of", res_types.keys(), "Got", res_type)
    if res_type not in res_types.keys():
        raise KeyError(
            f"There is not resource type {res_type} defined. Got",
            res_types.keys(),
        )


def find_aws_resource_arn_from_tags_api(
    info: dict,
    session: Session,
    aws_resource_search: str,
    types: dict = None,
    allow_multi: bool = False,
) -> Union[str, list[str]]:
    """
    Function to find the RDS DB based on info

    :param dict info:
    :param boto3.session.Session session: Boto3 session for clients
    :param str aws_resource_search: Resource type we are after within the AWS Service, ie. cluster, instance
    :param dict types: Additional types to match.
    :return:
    """
    res_types = deepcopy(ARNS_PER_TAGGINGAPI_TYPE)
    if types is not None and isinstance(types, dict):
        res_types.update(types)
    search_tags = (
        define_tagsgroups_filter_tags(info["Tags"]) if keyisset("Tags", info) else ()
    )
    name = info["Name"] if keyisset("Name", info) else None

    resources_r = get_resources_from_tags(session, aws_resource_search, search_tags)
    LOG.debug(search_tags)
    if not resources_r or not keyisset("ResourceTagMappingList", resources_r):
        resource_arns = []
    else:
        resource_arns = [
            i["ResourceARN"] for i in resources_r["ResourceTagMappingList"]
        ]
    return handle_search_results(
        resource_arns, name, res_types, aws_resource_search, allow_multi=allow_multi
    )


def assert_can_create_stack(client, name: str) -> bool:
    """
    Checks whether a stack already exists or not

    :raises: LookupError
    :raises: ClientError
    """
    try:
        stack_r = client.describe_stacks(StackName=name)
        if not keyisset("Stacks", stack_r):
            return True
        stacks = stack_r["Stacks"]
        if len(stacks) != 1:
            raise LookupError("Too many stacks found with stack name", name)
        stack = stacks[0]
        if stack["StackStatus"] == "REVIEW_IN_PROGRESS":
            return stack
        return False
    except ClientError as error:
        if (
            error.response["Error"]["Code"] == "ValidationError"
            and error.response["Error"]["Message"].find("does not exist") > 0
        ):
            return True
        raise error


def assert_can_update_stack(client, name) -> bool:
    """
    Checks whether a stack already exists or not
    """
    can_update_statuses = [
        "CREATE_COMPLETE",
        "ROLLBACK_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
    ]
    res = client.describe_stacks(StackName=name)
    if not res["Stacks"]:
        return False
    stack = res["Stacks"][0]
    LOG.info(stack["StackStatus"])
    if stack["StackStatus"] in can_update_statuses:
        return True
    return False


def validate_can_deploy_stack_from_settings(
    settings: ComposeXSettings, root_stack: ComposeXStack
) -> None:
    """
    Function to check that the stack can be updated

    :raises: ValueError
    """
    if not settings.upload:
        raise RuntimeError(
            "You selected --no-upload, which is incompatible with --deploy."
        )
    elif not root_stack.TemplateURL.startswith("https://"):
        raise ValueError(
            f"The URL for the stack is incorrect.: {root_stack.TemplateURL}",
            "TemplateURL must be a s3 URL",
        )


def deploy(settings: ComposeXSettings, root_stack: ComposeXStack) -> Union[str, None]:
    """
    Function to deploy (create or update) the stack to CFN.
    """
    validate_can_deploy_stack_from_settings(settings, root_stack)
    client = settings.session.client("cloudformation")
    if assert_can_create_stack(client, settings.name):
        res = client.create_stack(
            StackName=settings.name,
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
            Parameters=root_stack.render_parameters_list_cfn(),
            TemplateURL=root_stack.TemplateURL,
            DisableRollback=settings.disable_rollback,
        )
        LOG.info(f"Stack {settings.name} successfully deployed.")
        LOG.info(res["StackId"])
        return res["StackId"]
    elif assert_can_update_stack(client, settings.name):
        LOG.warning(f"Stack {settings.name} already exists. Updating.")
        res = client.update_stack(
            StackName=settings.name,
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
            Parameters=root_stack.render_parameters_list_cfn(),
            TemplateURL=root_stack.TemplateURL,
            DisableRollback=settings.disable_rollback,
        )
        LOG.info(f"Stack {settings.name} successfully updating.")
        LOG.info(res["StackId"])
        return res["StackId"]
    return None


def get_change_set_status(
    client, change_set_name: str, settings: ComposeXSettings
) -> str:
    """
    Function to determine whether we can create a new changeset.

    If it already exists in a failed status, we raise an exception to report we cannot go forward until user fixes it
    in their AWS account.
    If the changeset already exists, in a pending state, we wait for it to get to a ready status.
    If the changeset already exists, in a ready status, we dump a display of expected changes and return the status.

    """
    pending_statuses = [
        "CREATE_PENDING",
        "CREATE_IN_PROGRESS",
        "DELETE_PENDING",
        "DELETE_IN_PROGRESS",
        "REVIEW_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
    ]
    success_statuses = ["CREATE_COMPLETE", "DELETE_COMPLETE"]
    failed_statuses = ["DELETE_FAILED", "FAILED", "UPDATE_ROLLBACK_FAILED"]
    ready = False
    status = None
    while not ready:
        status = client.describe_change_set(
            ChangeSetName=change_set_name, StackName=settings.name
        )
        if status["Status"] in failed_statuses:
            raise SystemExit("Change set is unsucessful", status["Status"])
        if status["Status"] in pending_statuses:
            print(
                "ChangeSet creation in progress. Waiting 10 seconds",
                end="\r",
                flush=True,
            )
            sleep(10)
        elif status["Status"] in success_statuses:
            ready = True

    print(
        tabulate(
            [
                [
                    change["ResourceChange"]["LogicalResourceId"],
                    change["ResourceChange"]["ResourceType"],
                    change["ResourceChange"]["Action"],
                ]
                for change in status["Changes"]
            ],
            ["LogicalResourceId", "ResourceType", "Action"],
            tablefmt="rst",
        )
    )
    return status


def plan(
    settings: ComposeXSettings,
    root_stack: ComposeXStack,
    apply: bool = None,
    cleanup: bool = None,
) -> None:
    """
    Function to create a recursive change-set and return diffs
    """
    validate_can_deploy_stack_from_settings(settings, root_stack)
    client = settings.session.client("cloudformation")
    change_set_name = f"{settings.name}" + "_ecs_compose_x_" + dt.now().strftime("%s")
    if assert_can_create_stack(client, settings.name) or assert_can_update_stack(
        client, settings.name
    ):
        client.create_change_set(
            StackName=settings.name,
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
            Parameters=root_stack.render_parameters_list_cfn(),
            TemplateURL=root_stack.TemplateURL,
            UsePreviousTemplate=False,
            IncludeNestedStacks=True,
            ChangeSetType="CREATE",
            ChangeSetName=change_set_name,
        )
        status = get_change_set_status(client, change_set_name, settings)
        if status:
            plan_user_input(settings, client, change_set_name, apply, cleanup)


def plan_user_input(
    settings: ComposeXSettings,
    client,
    change_set_name: str,
    apply: bool = None,
    cleanup: bool = None,
) -> None:
    if apply is None:
        apply_q = input("Want to apply? [yN]: ")
        apply = apply_q.lower() in ["y", "yes"]

    if apply:
        client.execute_change_set(
            ChangeSetName=change_set_name,
            StackName=settings.name,
            DisableRollback=settings.disable_rollback,
        )
    else:
        if cleanup is None:
            delete_q = input("Cleanup ChangeSet ? [yN]: ")
            cleanup = delete_q.lower() in ["y", "yes"]

        if cleanup:
            client.delete_stack(StackName=settings.name)
