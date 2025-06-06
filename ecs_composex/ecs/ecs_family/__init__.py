# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2025 John Mille <john@compose-x.io>

"""
Package to manage an ECS "Family" Task and Service definition
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from troposphere.ecs import Service as CfnService
    from ecs_composex.common.settings import ComposeXSettings
    from ecs_composex.ecs.ecs_service import EcsService

import re
from itertools import chain

from troposphere import AWS_STACK_NAME, GetAtt, If, Join, NoValue
from troposphere import Output as CfnOutput
from troposphere import Ref, Region, Tags
from troposphere.ecs import (
    Environment,
    EphemeralStorage,
    RuntimePlatform,
    Secret,
    TaskDefinition,
)

from ecs_composex.common.logging import LOG
from ecs_composex.common.stacks import ComposeXStack
from ecs_composex.common.troposphere_tools import Parameter, add_outputs, add_parameters
from ecs_composex.compose.compose_services import ComposeService
from ecs_composex.ecs import ecs_conditions, ecs_params
from ecs_composex.ecs.ecs_family.family_helpers import (
    handle_same_task_services_dependencies,
)
from ecs_composex.ecs.ecs_family.family_logging import FamilyLogging
from ecs_composex.ecs.ecs_params import SERVICE_T, TASK_T
from ecs_composex.ecs.ecs_prometheus import set_prometheus
from ecs_composex.ecs.managed_sidecars.aws_xray import set_xray
from ecs_composex.ecs.service_compute import ServiceCompute
from ecs_composex.ecs.service_networking import ServiceNetworking
from ecs_composex.ecs.service_networking.helpers import (
    set_family_hostname,
    update_family_subnets,
)
from ecs_composex.ecs.service_scaling import ServiceScaling
from ecs_composex.ecs.task_compute import TaskCompute
from ecs_composex.ecs.task_iam import TaskIam

from .family_helpers import assign_secrets_to_roles, ensure_essential_containers
from .family_template import set_template
from .task_runtime import define_family_runtime_parameters


class ComposeFamily:
    """
    Class to group services logically to create the final ECS Task and Service definitions

    Processing order

    * Import first service
    * Define LaunchType
    * Define CapacityProviders if set
        This helps determine if we run in EXTERNAL mode early, as a lot of networking settings won't apply.

    :ivar list[ecs_composex.compose.compose_services.ComposeService] services: List of the Services part of the family
    :ivar ecs_composex.ecs.ecs_service.Service ecs_service: ECS Service settings
    :ivar ecs_composex.ecs.task_iam.TaskIam iam_manager:
    :ivar TaskCompute task_compute: Task Compute manager
    """

    def __init__(self, services: list[ComposeService], family_name):
        self._compose_services: list[ComposeService] = services
        self.ordered_services: list[ComposeService] = services
        self.managed_sidecars = []
        self.name = family_name
        self.family_hostname = self.name.replace("_", "-").lower()
        self.services_depends_on: dict = {}
        self.template = set_template(self)
        self.stack: ServiceStack = ServiceStack(
            self.logical_name,
            stack_template=self.template,
        )
        self.logging = None
        self.umbrella_log_group = None
        self.firelens_service = None
        self.firelens_config_service = None
        self.cwagent_service = None
        self.xray_service = None
        self.task_definition = None
        self.service_tags = None
        self.enable_execute_command = False
        self.ecs_service: EcsService | None = None
        self.runtime_cpu_arch = None
        self.runtime_os_family = None
        self.outputs = []
        self.task_logging_options = {}
        self.alarms = {}
        self.predefined_alarms = {}
        self.target_groups = []
        self.iam_manager = TaskIam(self)
        self.iam_manager.init_update_policies()
        self.service_scaling = None
        self.service_networking: ServiceNetworking | None = None
        self.task_compute = None
        self.service_compute: ServiceCompute = ServiceCompute(self)
        self.set_enable_execute_command()
        set_family_hostname(self)

    @property
    def logical_name(self) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", self.name)

    @property
    def services(self) -> list[ComposeService]:
        return list(chain(self.managed_sidecars, self.ordered_services))

    @property
    def services_names(self) -> list[str]:
        return [_svc.name for _svc in self.ordered_services]

    @property
    def want_xray(self) -> bool:
        return any([service.x_ray for service in self.services])

    @property
    def service_definition(self) -> Union[None, CfnService]:
        if self.ecs_service and self.ecs_service.ecs_service:
            return self.ecs_service.ecs_service
        return None

    @property
    def service_name_param(self) -> Parameter:
        return Parameter(
            f"{self.logical_name}{SERVICE_T}", group_label="ECS Settings", Type="String"
        )

    @property
    def service_arn_param(self) -> Parameter:
        return Parameter(
            f"{self.logical_name}{SERVICE_T}Arn",
            group_label="ECS Settings",
            Type="String",
        )

    def init_family(self) -> None:
        """
        Initializes the family after all services in the docker-compose definition have been assigned.

        The only containers that might then be added will be sidecars which won't influence
        launch type, capacity providers or anything else than the ECS Task Definition (CPU/RAM | ProxySettings)
        """
        self.set_services_to_services_dependencies()
        self.set_update_containers_priority()

        define_family_runtime_parameters(self)

        self.task_compute = TaskCompute(self)
        self.service_scaling = ServiceScaling(self)

    def init_task_definition(self):
        """
        Initialize the ECS TaskDefinition

        * Sets Compute settings
        * Sets the TaskDefinition using current services/ContainerDefinitions
        * Update the logging configuration for the containers.
        """
        self.task_compute.set_task_compute_parameter()
        self.set_task_definition()

    def set_task_definition(self):
        """
        Function to set or update the task definition

        :param self: the self of services
        """
        self.task_definition = TaskDefinition(
            TASK_T,
            template=self.template,
            Cpu=If(
                ecs_conditions.USE_FARGATE_CON_T,
                ecs_params.FARGATE_CPU,
                self.task_compute.cfn_family_cpu,
            ),
            Memory=If(
                ecs_conditions.USE_FARGATE_CON_T,
                ecs_params.FARGATE_RAM,
                self.task_compute.cfn_family_ram,
            ),
            NetworkMode=If(
                ecs_conditions.USE_WINDOWS_OS_T,
                NoValue,
                If(
                    ecs_conditions.USE_FARGATE_CON_T,
                    "awsvpc",
                    Ref(ecs_params.NETWORK_MODE),
                ),
            ),
            EphemeralStorage=(
                If(
                    ecs_conditions.USE_FARGATE_CON_T,
                    EphemeralStorage(SizeInGiB=self.task_ephemeral_storage),
                    NoValue,
                )
                if self.task_ephemeral_storage >= 21
                else NoValue
            ),
            InferenceAccelerators=NoValue,
            IpcMode=If(
                ecs_conditions.USE_WINDOWS_OS_T,
                NoValue,
                If(
                    ecs_conditions.USE_EC2_OR_EXTERNAL_LT_CON_T,
                    Ref(ecs_params.IPC_MODE),
                    NoValue,
                ),
            ),
            Family=Ref(ecs_params.SERVICE_NAME),
            TaskRoleArn=self.iam_manager.task_role.arn,
            ExecutionRoleArn=self.iam_manager.exec_role.arn,
            ContainerDefinitions=[s.container_definition for s in self.services],
            RequiresCompatibilities=ecs_conditions.use_external_lt_con(
                ["EXTERNAL"],
                If(
                    ecs_conditions.USE_FARGATE_CON_T,
                    ["FARGATE"],
                    If(ecs_conditions.USE_EC2_CON_T, ["EC2"], ["EC2", "FARGATE"]),
                ),
            ),
            RuntimePlatform=If(
                ecs_conditions.USE_FARGATE_CON_T,
                RuntimePlatform(
                    CpuArchitecture=Ref(ecs_params.RUNTIME_CPU_ARCHITECTURE),
                    OperatingSystemFamily=Ref(ecs_params.RUNTIME_OS_FAMILY),
                ),
                NoValue,
            ),
            Tags=Tags(
                {
                    "Name": Ref(ecs_params.SERVICE_NAME),
                    "Environment": Ref(AWS_STACK_NAME),
                    "compose-x::family": self.name,
                    "compose-x::logical_name": self.logical_name,
                }
            ),
        )
        for service in self.services:
            service.container_definition.DockerLabels.update(
                {
                    "container_name": service.container_name,
                    "ecs_task_family": Ref(ecs_params.SERVICE_NAME),
                }
            )

    def import_all_sidecars(self) -> None:
        """
        Once all services have been added from the ComposeXSettings looping over services, we import all sidecars
        Should be invoked only once.
        """
        set_xray(self)
        set_prometheus(self)
        self.set_services_family_links()

    def set_services_family_links(self):
        for service in self.ordered_services:
            if service.links:
                for link in service.links:
                    for _svc in self.ordered_services:
                        if _svc == service:
                            continue
                        if _svc.name in link:
                            service.family_links.append(link)
            if self.xray_service and self.xray_service.name not in service.family_links:
                service.family_links.append(self.xray_service.name)
            if (
                self.cwagent_service
                and self.cwagent_service.name not in service.family_links
            ):
                service.family_links.append(f"{self.cwagent_service.name}:cwagent")
            if service.family_links:
                setattr(
                    service.container_definition,
                    "Links",
                    If(
                        ecs_conditions.USE_WINDOWS_OS_T,
                        NoValue,
                        If(
                            ecs_conditions.USE_BRIDGE_NETWORKING_MODE_CON_T,
                            service.family_links,
                            NoValue,
                        ),
                    ),
                )

    def generate_outputs(self):
        """
        Generates a list of CFN outputs for the ECS Service and Task Definition
        """
        if (
            self.service_compute.launch_type != "EXTERNAL"
            and self.service_networking.security_group
        ):
            self.outputs.append(
                CfnOutput(
                    f"{self.logical_name}GroupId",
                    Value=Ref(self.service_networking.security_group.parameter.title),
                )
            )
        if (
            self.service_networking.subnets_output
            and isinstance(self.service_networking.subnets_output, Ref)
            and self.service_compute.launch_type != "EXTERNAL"
        ):
            self.outputs.append(
                CfnOutput(
                    ecs_params.SERVICE_SUBNETS.title,
                    Value=Join(",", self.service_networking.subnets_output),
                )
            )

        self.outputs.append(
            CfnOutput(self.task_definition.title, Value=Ref(self.task_definition))
        )
        if self.service_definition:
            self.outputs.append(
                CfnOutput(
                    self.service_name_param.title,
                    Value=GetAtt(self.service_definition, "Name"),
                )
            )
            self.outputs.append(
                CfnOutput(
                    self.service_arn_param.title,
                    Value=Ref(self.service_definition),
                )
            )
        if (
            self.service_scaling
            and self.service_scaling.scalable_target
            and self.service_scaling.scalable_target.title in self.template.resources
        ):
            self.outputs.append(
                CfnOutput(
                    self.service_scaling.scalable_target.title,
                    Value=Ref(self.service_scaling.scalable_target),
                )
            )
        add_outputs(self.template, self.outputs)

    def state_facts(self):
        """
        Function to display facts about the family.
        Similar to __repr__ but for logging the properties of the ComposeFamily
        """
        LOG.info(f"{self.name} - Hostname set to {self.family_hostname}")
        LOG.info(f"{self.name} - Ephemeral storage: {self.task_ephemeral_storage}")
        LOG.info(f"{self.name} - LaunchType set to {self.service_compute.launch_type}")
        LOG.info(
            f"{self.name} - TaskDefinition containers: "
            f"{[svc.name for svc in self.services]}"
        )

    def add_service(self, service: ComposeService):
        """
        Function to add new services (defined in the compose files). Not to use for managed sidecars
        :param ComposeService service:
        """

        self._compose_services.append(service)

        self.set_update_containers_priority()
        self.iam_manager.init_update_policies()
        # self.handle_logging()

        if self.task_definition and service.container_definition:
            self.task_definition.ContainerDefinitions.append(
                service.container_definition
            )
            self.set_secrets_access()
        self.set_enable_execute_command()
        set_family_hostname(self)

    def add_managed_sidecar(self, service: ComposeService):
        """
        Adds a new container/service to the Task Family and validates all settings that go along with the change.
        :param service:
        """

        if not isinstance(service, ComposeService) or not issubclass(
            type(service), ComposeService
        ):
            raise TypeError("service must be", ComposeService, "Got", type(service))
        if self.managed_sidecars and service.name in [
            svc.name for svc in self.managed_sidecars
        ]:
            LOG.debug(
                f"{self.name} - container service {service.name} is already set. Skipping"
            )
            return
        self.managed_sidecars.append(service)
        if self.task_definition and service.container_definition:
            self.task_definition.ContainerDefinitions.append(
                service.container_definition
            )
            self.set_secrets_access()
        self.iam_manager.init_update_policies()
        # self.handle_logging()
        self.task_compute.set_task_compute_parameter()

    def finalize_services_networking_settings(self, settings: ComposeXSettings) -> None:
        """
        Final pass on the service network settings
        """
        if settings.networks and self.service_networking.networks:
            update_family_subnets(self, settings)
        for service in chain(self.managed_sidecars, self.ordered_services):
            if service.ports or service.expose_ports:
                setattr(
                    service.container_definition,
                    "PortMappings",
                    service.define_port_mappings(self),
                )

    def init_network_settings(
        self, settings: ComposeXSettings, vpc_stack: ComposeXStack, families_sg_stack
    ) -> None:
        """
        Once we have figured out the compute settings (EXTERNAL vs other)
        """

        self.service_networking = ServiceNetworking(self, families_sg_stack)
        self.finalize_services_networking_settings(settings)
        if self.service_compute.launch_type == "EXTERNAL":
            LOG.debug(f"{self.name} Ingress cannot be set (EXTERNAL mode). Skipping")
        else:
            if vpc_stack.vpc_resource.mappings:
                self.stack.set_vpc_params_from_vpc_lookup(vpc_stack, settings)
            else:
                self.stack.set_vpc_parameters_from_vpc_stack(vpc_stack, settings)
            self.service_networking.ingress.set_aws_sources_ingress(
                settings,
                self.logical_name,
                Ref(self.service_networking.security_group.parameter.title),
            )
            self.service_networking.ingress.set_ext_sources_ingress(
                self.logical_name,
                Ref(self.service_networking.security_group.parameter.title),
            )
            self.service_networking.ingress.associate_aws_ingress_rules(self.template)
            self.service_networking.ingress.associate_ext_ingress_rules(self.template)
            self.service_networking.add_self_ingress()

    def finalize_family_settings(self, settings: ComposeXSettings):
        """
        Once all services have been added, we add the sidecars and deal with appropriate permissions and settings
        Will add xray / prometheus sidecars
        """
        from ecs_composex.ecs.ecs_family.family_helpers import (
            set_service_dependency_on_all_iam_policies,
        )
        from ecs_composex.ecs.ecs_family.family_helpers.compute_finalizers import (
            finalize_family_compute,
            finalize_scaling_settings,
        )
        from ecs_composex.ecs.ecs_family.family_helpers.network_finalizers import (
            finalize_lb_settings,
            finalize_network_settings,
        )

        finalize_network_settings(self, settings)
        finalize_family_compute(self)

        set_service_dependency_on_all_iam_policies(self)
        finalize_lb_settings(self)
        finalize_scaling_settings(self)
        self.generate_outputs()
        service_configs = [
            [0, service]
            for service in chain(self.managed_sidecars, self.ordered_services)
        ]
        handle_same_task_services_dependencies(service_configs)
        self.set_add_region_when_external()
        self.sort_secrets_env_vars()

    def composed_env_processing(self, settings: ComposeXSettings) -> None:
        for service in self.services:
            service.composed_env_processing(settings)

    def set_add_region_when_external(self):
        from troposphere.ecs import Environment

        env_var_to_add = Environment(Name="AWS_DEFAULT_REGION", Value=Region)
        region_conditional = If(
            ecs_conditions.USE_EXTERNAL_LT_T, env_var_to_add, NoValue
        )
        for service in self.services:
            environment = getattr(service.container_definition, "Environment")
            if (
                not environment
                or environment == NoValue
                or not isinstance(environment, list)
            ):
                environment = []
                setattr(service.container_definition, "Environment", environment)
            if "AWS_DEFAULT_REGION" not in [
                _env.Name for _env in environment if isinstance(_env, Environment)
            ]:
                environment.append(region_conditional)

    @staticmethod
    def sort_secrets(service: ComposeService, secrets: list) -> None:
        """Sorts secrets by Name"""
        if not secrets:
            return
        strictly_secrets: list = []
        non_secret_type: list = []
        for _secret in secrets:
            if isinstance(_secret, Secret):
                strictly_secrets.append(_secret)
            else:
                non_secret_type.append(_secret)
        sorted_secrets = sorted(strictly_secrets, key=lambda _secret: _secret.Name)
        sorted_secrets += non_secret_type
        if sorted_secrets:
            setattr(service.container_definition, "Secrets", sorted_secrets)
        else:
            setattr(service.container_definition, "Secrets", NoValue)

    @staticmethod
    def sort_env_vars(
        service: ComposeService, environment: list, secrets: list = None
    ) -> None:
        """
        Sorts env vars. If there are secrets in the list,
        checks to remove env vars with Name that'd overlap with an existing secret.
        Favoring secret over environment variable for security, as it's likely more sensitive.
        """
        strictly_env_vars: list = []
        non_env_vars: list = []
        for _env in environment:
            if isinstance(_env, Environment):
                strictly_env_vars.append(_env)
            else:
                non_env_vars.append(_env)
        sorted_env = sorted(strictly_env_vars, key=lambda _env_var: _env_var.Name)
        if sorted_env and (secrets and isinstance(secrets, list)):
            secrets_names: list[str] = [
                _secret.Name
                for _secret in getattr(service.container_definition, "Secrets", [])
            ]
            # Iterate in reverse for popping so we don't mess up indexes
            for _index, _env in reversed(tuple(enumerate(sorted_env))):
                if _env.Name in secrets_names:
                    LOG.warning(
                        "services.{}: Environment variable {} overlaps with Secret. Removing.".format(
                            service.family.name, _env.Name
                        )
                    )
                    sorted_env.pop(_index)
        sorted_env += non_env_vars
        if sorted_env:
            setattr(service.container_definition, "Environment", sorted_env)
        else:
            setattr(service.container_definition, "Environment", NoValue)

    def sort_secrets_env_vars(self):
        """
        Sorts secrets and env vars alphabetically.
        Removes env vars which would have a Key common to secrets
        """
        for service in self.services:
            secrets: list = getattr(service.container_definition, "Secrets", [])
            if secrets:
                self.sort_secrets(service, secrets)
            environment: list = getattr(service.container_definition, "Environment", [])
            if environment:
                self.sort_env_vars(
                    service,
                    environment,
                    getattr(service.container_definition, "Secrets", []),
                )

    def set_services_to_services_dependencies(self):
        """
        Method to iterate over each depends_on service set in the family services and add them up

        :return:
        """
        for service in self.services:
            if service.depends_on:
                if not isinstance(service.depends_on, dict):
                    raise TypeError(
                        "Service depends_on not a dict",
                        service.name,
                        service.depends_on,
                    )
                for service_name, condition_def in service.depends_on.items():
                    if service_name not in self.services_depends_on:
                        self.services_depends_on[service_name] = {}

    @property
    def task_ephemeral_storage(self) -> int:
        """
        If any service ephemeral storage is defined above, sets the ephemeral storage to the maximum of them.
        Return 0 if below 21 which is the default "free" Fargate storage space.
        """
        max_storage = max(service.ephemeral_storage for service in self.services)
        return max_storage if max_storage >= 21 else 0

    def set_enable_execute_command(self) -> None:
        """
        Sets necessary settings to enable ECS Execute Command
        ECS Anywhere support since 2022-01-24
        """
        from .task_execute_command import set_enable_execute_command

        set_enable_execute_command(self)

    def apply_ecs_execute_command_permissions(self, settings: ComposeXSettings) -> None:
        """
        Method to set the IAM Policies in place to allow ECS Execute SSM and Logging

        :param settings:
        :return:
        """
        from .task_execute_command import apply_ecs_execute_command_permissions

        apply_ecs_execute_command_permissions(self, settings)

    def handle_alarms(self) -> None:
        from ecs_composex.ecs.service_alarms import handle_alarms

        handle_alarms(self)

    def handle_logging(self, settings: ComposeXSettings):
        """
        Method to go over each service logging configuration and accordingly define the IAM permissions needed for
        the exec/task role
        """
        self.logging = FamilyLogging(self)
        self.logging.init_family_services_log_configuration()
        wants_firelens = [
            service
            for service in self.ordered_services
            if service.logging.uses_firelens
        ]
        self.logging.handle_awslogs_logging(wants_firelens)
        if wants_firelens:
            self.logging.handle_firelens(settings)
        self.logging.update_cw_log_retention()

    def set_update_containers_priority(self) -> None:
        """
        Method to sort out the containers dependencies and create the containers definitions based on the configs.
        """
        service_configs = [
            [0, service]
            for service in list(chain(self._compose_services, self.managed_sidecars))
        ]
        handle_same_task_services_dependencies(service_configs)
        ordered_containers_config = sorted(service_configs, key=lambda i: i[0])
        self.ordered_services = [s[1] for s in ordered_containers_config]
        ensure_essential_containers(self)

    def set_secrets_access(self):
        """
        Method to handle secrets permissions access
        """
        if not self.iam_manager.exec_role or not self.iam_manager.task_role:
            return
        secrets = []
        for service in self.services:
            for secret in service.secrets:
                secrets.append(secret)
        if secrets:
            assign_secrets_to_roles(
                secrets,
                self.iam_manager.exec_role.cfn_resource,
                self.iam_manager.task_role.cfn_resource,
            )

    def add_containers_images_cfn_parameters(self):
        """
        Adds parameters to the stack and set values for each service/container in the family definition
        """
        if not self.template or not self.stack:
            return
        images_parameters = []
        for service in chain(self.managed_sidecars, self.ordered_services):
            if service.image.image_param.title not in self.stack.Parameters:
                if isinstance(service.image.image, str):
                    self.stack.Parameters.update(
                        {service.image.image_param.title: service.image.image}
                    )
                elif isinstance(service.image.image, Ref):
                    LOG.debug(f"{service.name} image is Parameter already.")
                images_parameters.append(service.image.image_param)
        add_parameters(self.template, images_parameters)

    def validate_compute_configuration_for_task(self, settings):
        from ecs_composex.ecs_cluster.ecs_family_helpers import (
            validate_compute_configuration_for_task,
        )

        validate_compute_configuration_for_task(self, settings)

    def x_environment_processing(self):
        """
        Checks for each service if `x-environment` was set
        """
        from .family_helpers import swap_environment_value_with_parameter

        for service in self.ordered_services:
            if not service.x_environment:
                continue
            swap_environment_value_with_parameter(self, service)


class ServiceStack(ComposeXStack):
    """
    Class to identify specifically a service stack
    """
