# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille <john@compose-x.io>


from troposphere import MAX_OUTPUTS

import ecs_composex.common.troposphere_tools
from ecs_composex.common.stacks import ComposeXStack
from ecs_composex.common.troposphere_tools import add_outputs, build_template

CFN_MAX_OUTPUTS = MAX_OUTPUTS - 10


def create_kms_template(template, new_keys, xstack):
    """
    Function to create all the KMS Keys based on their definition

    :param ecs_composex.common.settings.ComposeXSettings settings:
    """
    mono_template = False
    if len(new_keys) <= CFN_MAX_OUTPUTS:
        mono_template = True

    for key in new_keys:
        key.stack = xstack
        key.define_kms_key()
        if key and key.cfn_resource:
            key.init_outputs()
            key.generate_outputs()
            if mono_template:
                template.add_resource(key.cfn_resource)
                key.handle_key_settings(template)
                add_outputs(template, key.outputs)
            elif not mono_template:
                key_template = build_template(
                    f"Template for KMS key {key.logical_name}"
                )
                key_template.add_resource(key.cfn_resource)
                key.handle_key_settings(key_template)
                add_outputs(template, key.outputs)
                key_stack = ComposeXStack(key.logical_name, stack_template=key_template)
                template.add_resource(key_stack)
