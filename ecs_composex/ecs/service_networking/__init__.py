# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille <john@compose-x.io>

"""
Module to help with defining the network settings for the ECS Service based on the family services definitions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ecs_composex.ecs.ecs_family import ComposeFamily
    from ecs_composex.cloudmap.cloudmap_ecs import EcsDiscoveryService
    from ecs_composex.ecs_ingress.ecs_ingress_stack import (
        XStack as EcsIngressStack,
        ServiceSecurityGroup,
    )
    from ecs_composex.common.settings import ComposeXSettings
    from troposphere.ecs import ServiceConnectConfiguration

from itertools import chain

from compose_x_common.compose_x_common import keyisset, set_else_none
from troposphere import (
    AWS_ACCOUNT_ID,
    AWSHelperFn,
    FindInMap,
    GetAtt,
    NoValue,
    Ref,
    Sub,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import AwsvpcConfiguration, NetworkConfiguration

from ecs_composex.common.cfn_params import Parameter
from ecs_composex.common.logging import LOG
from ecs_composex.common.troposphere_tools import add_parameters
from ecs_composex.ecs.ecs_conditions import use_external_lt_con
from ecs_composex.ecs.ecs_params import NETWORK_MODE, SERVICE_NAME
from ecs_composex.ecs.service_networking.ingress_helpers import (
    import_set_ecs_connect_settings,
    merge_cloudmap_settings,
    merge_family_services_networking,
)
from ecs_composex.ingress_settings import Ingress, set_service_ports
from ecs_composex.vpc.vpc_params import APP_SUBNETS


class ServiceNetworking:
    """
    Class to group the configuration for Service network settings

    :ivar list[dict] ports: List of the ports used by te service
    :ivar dict networks: Mapping of the networks to use for service
    """

    self_key = "Myself"

    def __init__(self, family: ComposeFamily, families_sg_stack: EcsIngressStack):
        """
        Initialize network settings for the family ServiceConfig

        :param ecs_composex.ecs.ecs_family.ComposeFamily family:
        """
        self.family = family
        self._network_mode = "awsvpc"
        self._sd_service = None
        if family.service_compute.launch_type == "EXTERNAL":
            LOG.warning(
                f"{family.name} - External mode cannot use awsvpc mode. Falling back to bridge"
            )
            self.network_mode = "bridge"
        self.ports = []
        self.networks = {}
        self.merge_services_ports()
        self.merge_networks()
        self.definition = merge_family_services_networking(family)
        self.security_group: ServiceSecurityGroup = families_sg_stack.services_mappings[
            family
        ]
        self.extra_security_groups = [self.security_group.parameter]
        self._subnets = Ref(APP_SUBNETS)
        self.cloudmap_config = (
            merge_cloudmap_settings(family, self.ports) if self.ports else {}
        )
        self.ecs_connect_config: ServiceConnectConfiguration | None = None
        self.ingress = Ingress(self.definition[Ingress.master_key], self.ports)

    @property
    def ingress_from_self(self) -> bool:
        if self.ingress:
            return keyisset(self.self_key, self.ingress.definition)
        return False

    @property
    def ecs_network_config(self):
        if self.family.service_compute.launch_type == "EXTERNAL":
            return NoValue
        return use_external_lt_con(
            NoValue,
            NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=self.subnets,
                    SecurityGroups=self.security_groups,
                    AssignPublicIp=self.eip_assign,
                )
            ),
        )

    @property
    def sd_service(self):
        return self._sd_service

    @sd_service.setter
    def sd_service(self, sd_service: EcsDiscoveryService):
        self._sd_service = sd_service
        if self.family.ecs_service and not self.family.ecs_service.registries:
            self.family.ecs_service.registries.append(
                self._sd_service.ecs_service_registry
            )
        else:
            setattr(
                self.family.ecs_service,
                "registries",
                [self._sd_service.ecs_service_registry],
            )

    @property
    def eip_assign(self):
        if any([svc.eip_auto_assign for svc in self.family.ordered_services]):
            LOG.info(
                f"{self.family.name} - networking - "
                "At least one service in definition has AssignPublicIp set to True."
            )
            return "ENABLED"
        return "DISABLED"

    @property
    def security_groups(self) -> list:
        groups = [Ref(self.security_group.parameter.title)]
        for extra_group in self.extra_security_groups:
            if (
                isinstance(extra_group, SecurityGroup)
                and extra_group.title in self.family.template.resources
            ):
                groups.append(Ref(extra_group))
            elif isinstance(extra_group, Parameter):
                add_parameters(self.family.template, [extra_group])
                groups.append(Ref(extra_group))
            elif isinstance(extra_group, FindInMap):
                groups.append(extra_group)
        return groups

    @property
    def network_mode(self):
        """
        The network mode used for the Task/Service. valid are host/bridge/awsvpc.
        Defaults to awsvpc. Only override is to bridge/host based on the Launch Type
        """
        return self._network_mode

    @network_mode.setter
    def network_mode(self, mode: str):
        self._network_mode = mode
        if self.family.stack:
            self.family.stack.Parameters.update(
                {NETWORK_MODE.title: self._network_mode}
            )

    @property
    def subnets(self):
        return self._subnets

    @property
    def subnets_output(self):
        if isinstance(self.subnets, Ref):
            return self.subnets

    @subnets.setter
    def subnets(self, value):
        """
        Subnets value should only be a Ref on parameter or a CFN Function.
        If successful, auto updates the NetworkConfiguration for the family ecs_service
        """
        if isinstance(value, Parameter):
            self._subnets = Ref(Parameter)
        elif issubclass(type(value), AWSHelperFn):
            self._subnets = value
        self._subnets = value
        if self.family.ecs_service and self.family.ecs_service.ecs_service:
            setattr(
                self.family.ecs_service.ecs_service,
                "NetworkConfiguration",
                self.ecs_network_config,
            )

    def merge_networks(self):
        """
        Method to merge network
        """
        for svc in self.family.ordered_services:
            if svc.networks:
                self.networks.update(svc.networks)

    def merge_services_ports(self):
        """Function to merge two sections of ports"""
        source_ports = [
            service.ports
            for service in chain(
                self.family.managed_sidecars, self.family.ordered_services
            )
            if service.ports
        ]
        for port_set in source_ports:
            f_source_ports = set_service_ports(self.ports)
            f_override_ports = set_service_ports(port_set)
            self.ports = []
            f_overide_ports_targets = [port["target"] for port in f_override_ports]
            for port in f_override_ports:
                self.ports.append(port)
                for s_port in f_source_ports:
                    if s_port["target"] not in f_overide_ports_targets:
                        self.ports.append(s_port)

    def set_ecs_connect(self, settings: ComposeXSettings):
        self.ecs_connect_config = import_set_ecs_connect_settings(self.family, settings)

    def add_self_ingress(self) -> None:
        """
        Method to allow communications internally to the group on set ports
        """
        LOG.debug(f"SELF INGRESS? {self.family.name} {self.ingress_from_self}")
        if (
            not self.family.template
            or not self.family.ecs_service
            or not self.ingress_from_self
        ):
            return
        for port in self.ports:
            target_port = set_else_none(
                "published", port, alt_value=set_else_none("target", port, None)
            )
            if target_port is None:
                raise ValueError(
                    "Wrong port definition value for security group ingress", port
                )
            self.ingress.to_self_rules.append(
                SecurityGroupIngress(
                    f"AllowingInterCommunicationPort{target_port}{port['protocol']}",
                    template=self.family.template,
                    FromPort=target_port,
                    ToPort=target_port,
                    IpProtocol=port["protocol"],
                    GroupId=Ref(
                        self.family.service_networking.security_group.parameter
                    ),
                    SourceSecurityGroupId=Ref(
                        self.family.service_networking.security_group.parameter
                    ),
                    SourceSecurityGroupOwnerId=Ref(AWS_ACCOUNT_ID),
                    Description=Sub(
                        f"Internal traffic on {target_port}/{port['protocol']}"
                    ),
                )
            )

    def add_lb_ingress(self, lb_name, lb_sg_ref) -> None:
        """
        Method to add ingress rules from other AWS Sources

        :param str lb_name:
        :param lb_sg_ref:
        :return:
        """
        if not self.family.template or not self.family.ecs_service:
            return
        for port in self.ports:
            title = f"FromLB{lb_name}To{self.family.stack.title}On{port['target']}"
            common_args = {
                "FromPort": port["target"],
                "ToPort": port["target"],
                "IpProtocol": port["protocol"],
                "GroupId": Ref(self.security_group.parameter.title),
                "SourceSecurityGroupOwnerId": Ref(AWS_ACCOUNT_ID),
                "Description": Sub(
                    f"From ELB {lb_name} to ${{{SERVICE_NAME.title}}} on port {port['target']}"
                ),
            }
            if title in self.family.template.resources:
                return
            SecurityGroupIngress(
                title,
                template=self.family.template,
                SourceSecurityGroupId=lb_sg_ref,
                **common_args,
            )
