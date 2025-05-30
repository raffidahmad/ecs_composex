# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille <john@compose-x.io>

"""Common Conditions across the templates"""

from troposphere import Equals, If, Ref

from ecs_composex.common import cfn_params

USE_STACK_NAME_CON_T = "UseStackName"
USE_STACK_NAME_CON = Equals(
    Ref(cfn_params.ROOT_STACK_NAME), cfn_params.ROOT_STACK_NAME.Default
)


def pass_root_stack_name():
    """
    Function to add root_stack to a stack parameters

    :return: rootstack name value based on condition
    """
    return {
        cfn_params.ROOT_STACK_NAME_T: If(
            USE_STACK_NAME_CON_T,
            Ref("AWS::StackName"),
            Ref(cfn_params.ROOT_STACK_NAME),
        )
    }


def define_stack_name(template=None):
    """
    Function to return Stack name construct.
    Adds the conditions and parameters if template is given.

    :param troposphere.Template template: the template to add it to.
    :return:
    """
    if template and USE_STACK_NAME_CON_T not in template.conditions:
        template.add_condition(USE_STACK_NAME_CON_T, USE_STACK_NAME_CON)
    return If(
        USE_STACK_NAME_CON_T,
        Ref("AWS::StackName"),
        Ref(cfn_params.ROOT_STACK_NAME),
    )
