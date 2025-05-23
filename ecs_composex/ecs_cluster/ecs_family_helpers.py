#  SPDX-License-Identifier: MPL-2.0
#  Copyright 2020-2025 John Mille <john@compose-x.io>

from __future__ import annotations

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ecs_composex.common.settings import ComposeXSettings
    from ecs_composex.ecs.ecs_family import ComposeFamily
    from ecs_composex.ecs_cluster import EcsCluster

from troposphere import NoValue

from ecs_composex.ecs.ecs_params import LAUNCH_TYPE
from ecs_composex.ecs_cluster import FARGATE_PROVIDERS
from ecs_composex.ecs_composex import LOG

"""
Module to set the Launch Type / Capacity providers of ComposeFamily according to the ECS Cluter settings
and x-ecs settings
"""


def validate_capacity_providers(family: ComposeFamily, cluster: EcsCluster) -> bool:
    """
    Validates that the defined ecs_capacity_providers are all available in the ECS Cluster Providers

    :raises: ValueError if not all task family providers in the cluster providers
    :raises: TypeError if cluster_providers not a list
    """
    if (
        not family.service_compute.ecs_capacity_providers
        and not cluster.capacity_providers
    ):
        LOG.debug(
            f"{family.name} - No capacity providers specified in task definition nor cluster"
        )
        return True
    elif not cluster.capacity_providers:
        LOG.debug(f"{family.name} - No capacity provider set for cluster")
        return True
    cap_names = [
        cap.CapacityProvider for cap in family.service_compute.ecs_capacity_providers
    ]
    if not all(cap_name in FARGATE_PROVIDERS for cap_name in cap_names):
        raise ValueError(
            f"{family.name} - You cannot mix FARGATE capacity provider with AutoScaling Capacity Providers",
            cap_names,
        )
    if not isinstance(cluster.capacity_providers, list):
        raise TypeError("clusters_providers must be a list")

    elif not all(provider in cluster.capacity_providers for provider in cap_names):
        raise ValueError(
            "Providers",
            cap_names,
            "not defined in ECS Cluster providers. Available providers are",
            cluster.capacity_providers,
        )


def validate_compute_configuration_for_task(
    family: ComposeFamily, settings: ComposeXSettings
) -> None:
    """Function to perform a final validation of compute before rendering."""
    if (
        family.service_compute.launch_type
        and family.service_compute.launch_type == "EXTERNAL"
    ):
        LOG.debug(f"{family.name} - Launch Type set to EXTERNAL. Nothing to do.")
        return
    if settings.ecs_cluster.platform_override:
        family.service_compute.launch_type = settings.ecs_cluster.platform_override
        LOG.warning(
            f"{family.name} - Due to Launch Type override to {settings.ecs_cluster.platform_override}"
            ", ignoring CapacityProviders"
            f"{[_cap.CapacityProvider for _cap in family.service_compute.ecs_capacity_providers]}"
        )
        if family.service_definition:
            setattr(
                family.service_definition,
                "CapacityProviderStrategy",
                NoValue,
            )
    else:
        family.service_compute.set_update_launch_type()
        family.service_compute.set_update_capacity_providers()
        validate_capacity_providers(family, settings.ecs_cluster)
        if (
            not family.service_compute.ecs_capacity_providers
            and family.service_compute.launch_type in ["EC2", "EXTERNAL"]
        ):
            return
        set_service_launch_type(family, settings.ecs_cluster)
        LOG.debug(
            f"{family.name} - Updated {LAUNCH_TYPE.title} to"
            f" {family.service_compute.launch_type}"
        )


def set_launch_type_from_cluster_and_service(
    family: ComposeFamily, cluster: EcsCluster
) -> None:
    """
    Sets the launch type based on the service and capacity providers

    If all the capacity providers of the service are FARGATE, we use `FARGATE_PROVIDERS` which removes `LaunchType` from
    ECS Service definition
    Otherwise, we use the capacity providers set which use AutoScaling.
    """
    family_providers: list = [
        cap.CapacityProvider for cap in family.service_compute.ecs_capacity_providers
    ]
    family_uses_fargate_only = all(
        provider in FARGATE_PROVIDERS for provider in family_providers
    )
    cluster_uses_fargate_only = all(
        provider in FARGATE_PROVIDERS for provider in cluster.capacity_providers
    )
    if not all(provider in cluster.capacity_providers for provider in family_providers):
        raise AttributeError(
            "Family {} tries to use providers not available in the cluster. "
            "Wants: {}. Available: {}".format(
                family.name, family_providers, cluster.capacity_providers
            )
        )
    if family_uses_fargate_only and cluster_uses_fargate_only:
        family.service_compute.launch_type = "FARGATE_PROVIDERS"
    else:
        family.service_compute.launch_type = "SERVICE_MODE"
        LOG.info(
            f"{family.name} - Using AutoScaling Based Providers",
            [
                provider.CapacityProvider
                for provider in family.service_compute.ecs_capacity_providers
            ],
        )


def set_launch_type_from_cluster_only(
    family: ComposeFamily, cluster: EcsCluster
) -> None:
    """
    When the family x-ecs has not set CapacityProviders, we rely on the ECS Cluster definition.
    If all the capacity providers defined on the Cluster are FARGATE related, use `FARGATE_PROVIDERS`
    Otherwise, use the ECS Cluster defined capacity providers based on the Cluster strategy.
    """
    if any(
        provider in ["FARGATE", "FARGATE_SPOT"]
        for provider in cluster.default_strategy_providers
    ) or all(
        provider in cluster.capacity_providers
        for provider in ["FARGATE", "FARGATE_SPOT"]
    ):
        family.service_compute.launch_type = "FARGATE_PROVIDERS"
        LOG.debug(
            f"{family.name} - Defaulting to FARGATE_PROVIDERS as "
            "FARGATE[_SPOT] is found in the cluster default strategy"
        )
    else:
        family.service_compute.launch_type = "CLUSTER_MODE"
        LOG.debug(
            f"{family.name} - Cluster uses non Fargate Capacity Providers. Setting to Cluster default"
        )
        family.service_compute.launch_type = "CLUSTER_MODE"


def set_service_launch_type(family: ComposeFamily, cluster) -> None:
    """
    Sets the LaunchType value for the ECS Service
    If the LaunchType is EXTERNAL or EC2, we ignore Capacity Providers altogether.
    """
    if family.service_compute.launch_type in ["EXTERNAL", "EC2"]:
        LOG.debug(
            "services.{} uses {}. Skipping".format(
                family.name, family.service_compute.launch_type
            )
        )
        return
    if family.service_compute.ecs_capacity_providers and cluster.capacity_providers:
        set_launch_type_from_cluster_and_service(family, cluster)
    elif (
        not family.service_compute.ecs_capacity_providers and cluster.capacity_providers
    ):
        set_launch_type_from_cluster_only(family, cluster)
