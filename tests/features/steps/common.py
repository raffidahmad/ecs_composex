# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille<john@compose-x.io>

from os import path

from behave import given, then
from pytest import raises

from ecs_composex.common.settings import ComposeXSettings
from ecs_composex.common.stacks import process_stacks
from ecs_composex.ecs_composex import generate_full_template
from ecs_composex.exceptions import ComposeBaseException


def here():
    return path.abspath(path.dirname(__file__))


@given("With {file_path}")
def step_impl(context, file_path):
    if not hasattr(context, "files"):
        files = []
        setattr(context, "files", files)
    else:
        files = getattr(context, "files")
    files.append(file_path)


@given("I use defined files as input to define execution settings")
def step_impl(context):
    cases_path = [
        path.abspath(f"{here()}/../../../{file_name}") for file_name in context.files
    ]
    context.settings = ComposeXSettings(
        profile_name=(
            getattr(context, "profile_name")
            if hasattr(context, "profile_name")
            else None
        ),
        **{
            ComposeXSettings.name_arg: "test",
            ComposeXSettings.command_arg: ComposeXSettings.render_arg,
            ComposeXSettings.input_file_arg: cases_path,
            ComposeXSettings.format_arg: "yaml",
        },
    )
    context.settings.set_bucket_name_from_account_id()


@then("I render the docker-compose to composex to validate")
def step_impl(context):
    context.root_stack = generate_full_template(context.settings)


@then("I use defined files as input expecting an error")
def step_impl(context):
    cases_path = [
        path.abspath(f"{here()}/../../../{file_name}") for file_name in context.files
    ]
    print(cases_path)
    with raises(Exception):
        context.settings = ComposeXSettings(
            profile_name=(
                getattr(context, "profile_name")
                if hasattr(context, "profile_name")
                else None
            ),
            **{
                ComposeXSettings.name_arg: "test",
                ComposeXSettings.command_arg: ComposeXSettings.render_arg,
                ComposeXSettings.input_file_arg: cases_path,
                ComposeXSettings.format_arg: "yaml",
            },
        )
        context.settings.set_bucket_name_from_account_id()
        generate_full_template(context.settings)


@given("I use {file_path} as my docker-compose file")
def step_impl(context, file_path):
    """
    Function to import the Docker file from use-cases.

    :param context:
    :param str file_path:
    :return:
    """
    cases_path = path.abspath(f"{here()}/../../../{file_path}")

    context.settings = ComposeXSettings(
        profile_name=(
            getattr(context, "profile_name")
            if hasattr(context, "profile_name")
            else None
        ),
        **{
            ComposeXSettings.name_arg: "test",
            ComposeXSettings.command_arg: ComposeXSettings.render_arg,
            ComposeXSettings.input_file_arg: [cases_path],
            ComposeXSettings.format_arg: "yaml",
        },
    )
    context.settings.set_bucket_name_from_account_id()


@given(
    "I use {file_path} as my docker-compose file and {override_file} as override file"
)
def step_impl(context, file_path, override_file):
    """
    Function to import the Docker file from use-cases.

    :param context:
    :param str file_path:
    :param str override_file:
    :return:
    """
    cases_path = path.abspath(f"{here()}/../../../{file_path}")
    override_path = path.abspath(f"{here()}/../../../{override_file}")
    context.settings = ComposeXSettings(
        profile_name=(
            getattr(context, "profile_name")
            if hasattr(context, "profile_name")
            else None
        ),
        **{
            ComposeXSettings.name_arg: "test",
            ComposeXSettings.command_arg: ComposeXSettings.render_arg,
            ComposeXSettings.input_file_arg: [cases_path, override_path],
            ComposeXSettings.format_arg: "yaml",
        },
    )
    context.settings.set_bucket_name_from_account_id()


@then("I render all files to verify execution")
def set_impl(context):
    if not hasattr(context, "root_stack"):
        context.root_stack = generate_full_template(context.settings)
    print(context.settings.x_resources)
    process_stacks(context.root_stack, context.settings)


@given("I want to use aws profile {profile_name}")
def step_impl(context, profile_name):
    """
    Function to change the session to a specific one.
    """
    context.session_name = profile_name


@given("I want to upload files to S3 bucket {bucket_name}")
def step_impl(context, bucket_name):
    context.settings.upload = True
    context.settings.no_upload = False
    context.settings.bucket_name = bucket_name


@given("I set I did not want to upload")
def step_impl(context):
    context.settings.upload = False
    context.settings.no_upload = True


@then("I should have a stack ID")
def step_impl(context):
    """
    Function to check we got a stack ID
    """
    assert context.stack_id is not None


@then("I should not have a stack ID")
def step_impl(context):
    """
    Function to check we got a stack ID
    """
    assert context.stack_id is None


@then("I render the docker-compose expecting an error")
def step_impl(context):
    with raises((ValueError, KeyError, ComposeBaseException)):
        context.root_stack = generate_full_template(context.settings)


@then("With missing module from file, program quits with code {code:d}")
def step_impl(context, code):
    with raises(SystemExit) as exit_error:
        context.resource_type(context.settings.compose_content, context.settings)
    assert exit_error.value.code == code
