#  -*- coding: utf-8 -*-
# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille<john@compose-x.io>


from behave import then

from ecs_composex.common.stacks import ComposeXStack


@then("I should have a RDS DB")
def step_impl(context):
    """
    Function to ensure we have a RDS stack and a DB stack within
    :param context:
    :return:
    """
    template = context.root_stack.stack_template
    db_root_stack = template.resources["rds"]
    assert issubclass(type(db_root_stack), ComposeXStack)


@then("services have access to it")
def step_impl(context):
    """
    Function to ensure that the services have secret defined.
    """
