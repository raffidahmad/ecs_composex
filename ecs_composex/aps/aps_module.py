#  SPDX-License-Identifier: MPL-2.0
#  Copyright 2020-2025 John Mille <john@compose-x.io>

"""Managed Prometheus module"""

from os import path
from pathlib import Path

from ecs_composex.mods_manager import XResourceModule

from .aps_stack import ManagedPrometheus, XStack

COMPOSE_X_MODULES: dict = {
    "x-aps": {
        "Module": XResourceModule(
            "x-aps",
            XStack,
            Path(path.abspath(path.dirname(__file__))),
            ManagedPrometheus,
        ),
    },
}
