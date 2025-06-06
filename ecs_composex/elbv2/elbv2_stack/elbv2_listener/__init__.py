#  SPDX-License-Identifier: MPL-2.0
#  Copyright 2020-2025 John Mille <john@compose-x.io>

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ecs_composex.elbv2 import Elbv2
    from ecs_composex.elbv2.elbv2_ecs import MergedTargetGroup

import warnings
from copy import deepcopy

from compose_x_common.compose_x_common import keyisset
from troposphere import Ref
from troposphere.cognito import UserPoolClient
from troposphere.elasticloadbalancingv2 import Listener

from ecs_composex.common import NONALPHANUM
from ecs_composex.common.logging import LOG
from ecs_composex.common.troposphere_tools import add_parameters
from ecs_composex.elbv2.elbv2_params import LB_ARN
from ecs_composex.elbv2.elbv2_stack.helpers import (
    add_acm_certs_arn,
    define_actions,
    define_listener_rules_actions,
    handle_default_actions,
    handle_non_default_services,
    import_cognito_pool,
    import_new_acm_certs,
    map_service_target,
    tea_pot,
    validate_duplicate_targets,
)
from ecs_composex.resources_import import import_record_properties


class ComposeListener(Listener):
    attributes = [
        "Condition",
        "CreationPolicy",
        "DeletionPolicy",
        "DependsOn",
        "Metadata",
        "UpdatePolicy",
        "UpdateReplacePolicy",
    ]

    targets_keys = "Targets"

    def __init__(self, lb: Elbv2, port: int, definition: dict):
        """
        Method to init listener.

        :param ecs_composex.elbv2.elbv2_stack.elbv2.Elbv2 lb:
        :param dict definition:
        """
        self._lb = lb
        self._port: int = port
        self.definition = deepcopy(definition)
        straight_import_keys = ["Protocol", "SslPolicy", "AlpnPolicy"]
        listener_kwargs = {
            x: self.definition[x] for x in straight_import_keys if x in self.definition
        }
        listener_kwargs.update(
            {x: self.definition[x] for x in self.attributes if x in self.definition}
        )
        listener_kwargs.update({"Port": port})
        self.services = (
            self.definition[self.targets_keys]
            if keyisset(self.targets_keys, self.definition)
            and isinstance(self.definition[self.targets_keys], list)
            else []
        )
        self.default_actions = (
            self.definition["DefaultActions"]
            if keyisset("DefaultActions", self.definition)
            else []
        )
        if lb.cfn_resource:
            listener_kwargs.update({"LoadBalancerArn": Ref(lb.lb)})
        else:
            add_parameters(
                lb.stack.stack_template,
                [lb.attributes_outputs[LB_ARN]["ImportParameter"]],
            )
            listener_kwargs.update(
                {
                    "LoadBalancerArn": Ref(
                        lb.attributes_outputs[LB_ARN]["ImportParameter"]
                    )
                }
            )
            lb.stack.Parameters.update(
                {
                    lb.attributes_outputs[LB_ARN][
                        "ImportParameter"
                    ].title: lb.attributes_outputs[LB_ARN]["ImportValue"]
                }
            )
        self.name = f"{lb.logical_name}{listener_kwargs['Port']}"
        super().__init__(self.name, **listener_kwargs)
        self.DefaultActions = []

    @property
    def def_port(self) -> int:
        return self._port

    @property
    def lb(self) -> Elbv2:
        return self._lb

    def define_default_actions(self, lb: Elbv2, template):
        """
        If DefaultTarget is set it will set it if not a service, otherwise at the service level.
        If not defined, and there is more than one service, it will fail.
        If not defined and there is only one service defined, it will skip
        """
        rules: list = []
        if not self.default_actions and not self.services:
            warnings.warn(
                f"{self.name} - There are no actions defined or services for listener {self.title}. Skipping"
            )
            return
        if self.default_actions:
            handle_default_actions(self)
        elif not self.default_actions and self.services and len(self.services) == 1:
            if not keyisset("Conditions", self.services[0]):
                LOG.info(
                    f"{self.title} has no defined DefaultActions and only 1 service "
                    "without Conditions. Setting Listener DefaultActions to service."
                )
                self.DefaultActions = define_actions(self, self.services[0])
            else:
                self.DefaultActions = [tea_pot(True)]
                rules = define_listener_rules_actions(self, self.services)
        elif lb.is_nlb() and self.services and len(self.services) > 1:
            raise ValueError(
                f"{lb.module.res_key}.{lb.name} - Listener {self.def_port}"
                " - NLB cannot have more than one target per listener."
            )
        elif not self.default_actions and self.services and len(self.services) > 1:
            LOG.warning(
                f"{self.title} - "
                "No default actions defined and more than one service defined. "
                "If one of the access path is / it will be used as default"
            )
            rules = handle_non_default_services(self)
        else:
            raise ValueError(f"Failed to determine any default action for {self.title}")
        if rules and lb.is_alb():
            for rule in rules:
                template.add_resource(rule)
        else:
            LOG.warning(
                f"{lb.module.res_key}.{lb.name} - LB is NLB. Can't assign Listener Rules."
            )

    def handle_cognito_pools(self, settings, listener_stack):
        """

        :param ecs_composex.common.settings.ComposeXSettings settings:
        :param ecs_composex.common.stacks.ComposeXStack listener_stack:
        :return:
        """
        cognito_auth_key = "AuthenticateCognitoConfig"
        for target in self.services:
            if keyisset("CreateCognitoClient", target):
                user_pool_client_params = target["CreateCognitoClient"]
                pool_id = user_pool_client_params["UserPoolId"]
                pool_params = import_cognito_pool(pool_id, settings, listener_stack)
                user_pool_client_params["UserPoolId"] = pool_params[0]
                user_pool_client_props = import_record_properties(
                    user_pool_client_params, UserPoolClient
                )
                user_pool_client = listener_stack.stack_template.add_resource(
                    UserPoolClient(
                        f"{listener_stack.title}UserPoolClient{NONALPHANUM.sub('', target['name'])}",
                        **user_pool_client_props,
                    )
                )
                if keyisset(cognito_auth_key, target):
                    target[cognito_auth_key]["UserPoolArn"] = pool_params[1]
                    target[cognito_auth_key]["UserPoolDomain"] = pool_params[2]
                    target[cognito_auth_key]["UserPoolClientId"] = Ref(user_pool_client)
                else:
                    LOG.warning(
                        "No AuthenticateCognitoConfig defined. Setting to default settings"
                    )
                    target.update(
                        {
                            cognito_auth_key: {
                                "OnUnauthenticatedRequest": "authenticate",
                                "Scope": "openid email profile",
                                "UserPoolArn": pool_params[1],
                                "UserPoolDomain": pool_params[2],
                                "UserPoolClientId": Ref(user_pool_client),
                            }
                        }
                    )
                del target["CreateCognitoClient"]
            elif (
                not keyisset("CreateCognitoClient", target)
                and keyisset(cognito_auth_key, target)
                and keyisset("UserPoolArn", target[cognito_auth_key])
                and target[cognito_auth_key]["UserPoolArn"].startswith("x-cognito")
            ):
                pool_id = target[cognito_auth_key]["UserPoolArn"].split(r"::")[-1]
                pool_params = import_cognito_pool(pool_id, settings, listener_stack)
                target[cognito_auth_key]["UserPoolArn"] = pool_params[1]
                target[cognito_auth_key]["UserPoolDomain"] = pool_params[2]

    def handle_certificates(self, settings, listener_stack):
        """
        Method to handle certificates

        :param ecs_composex.common.settings.ComposeXSettings settings:
        :param listener_stack: The stack that has the listener as resource

        :return:
        """
        if not keyisset("Certificates", self.definition):
            LOG.warning(f"No certificates defined for Listener {self.name}")
            return
        valid_sources = [
            ("x-acm", str, import_new_acm_certs),
            ("Arn", str, add_acm_certs_arn),
            ("CertificateArn", str, add_acm_certs_arn),
        ]
        for cert_def in self.definition["Certificates"]:
            if isinstance(cert_def, dict):
                cert_source = list(cert_def.keys())[0]
                source_value = cert_def[cert_source]
                for src_type in valid_sources:
                    if (
                        src_type[0] == cert_source
                        and isinstance(cert_source, src_type[1])
                        and src_type[2]
                    ):
                        src_type[2](self, source_value, settings, listener_stack)

    def map_lb_target_groups_service_to_listener_targets(self, lb: Elbv2) -> None:
        """
        Map Services defined in LB definition to Targets
        """
        if not self.services:
            return
        validate_duplicate_targets(lb, self)
        for l_service_def in self.services:
            map_service_target(lb, l_service_def)
            if not keyisset("target_arn", l_service_def):
                raise LookupError(
                    f"{lb.module.res_key}.{lb.name} - Listener {self.name}",
                    f"Failed to map {l_service_def['name']} to any family:service:port combination",
                )

    def map_target_group_to_listener(self, target_group: MergedTargetGroup) -> None:
        if not self.services:
            LOG.warning(
                f"{self.lb.module.res_key}.{self.lb.name} - Listener {self.Port} - No Targets defined."
            )
            return
        for _tgt_group in self.services:
            _tgt_name = _tgt_group["name"]
            if _tgt_name == target_group.name:
                _tgt_group["target_arn"] = Ref(target_group)
                break
        else:
            LOG.debug(
                f"{self.lb.module.res_key}.{self.lb.name} - Listener {self.Port} - No target group matched."
            )
